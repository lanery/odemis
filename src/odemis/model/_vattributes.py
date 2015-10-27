# -*- coding: utf-8 -*-
"""
Created on 26 Mar 2012

@author: Éric Piel

Copyright © 2012 Éric Piel, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License version 2 as published by the Free
Software Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
Odemis. If not, see http://www.gnu.org/licenses/.
"""
from __future__ import division

import Pyro4
from Pyro4.core import oneway
import collections
import inspect
import logging
import numbers
import numpy
import threading
from types import NoneType
import zmq

from odemis.util.weak import WeakMethod, WeakRefLostError

from . import _core


class NotSettableError(AttributeError):
    pass


class NotApplicableError(Exception):
    pass


class VigilantAttributeBase(object):
    """
    An abstract class for VigilantAttributes and its proxy
    It needs a .value member
    """

    def __init__(self, initval=None, unit=None):
        """
        Creates a VigilantAttributeBase with a given initial value
        initval (any type): the initial value
        unit (str): a SI unit in which the VA is expressed
        """
        self._listeners = set()
        self._value = initval
        self.unit = unit

    def __str__(self):
        if self.unit is None:
            val = "%s" % (self._value,)
        else:
            val = "%s %s" % (self._value, self.unit)

        return "%s (value = %s) with %d subscribers" % (self.__class__.__name__,
                                                        val,
                                                        len(self._listeners))

    def subscribe(self, listener, init=False, **kwargs):
        """
        Register a callback function to be called when the VigilantAttributeBase
        is changed

        listener (function): callback function which takes as argument val the
            new value
        init (boolean): if True calls the listener directly, to initialise it

        Additional keyword arguments can be provided. They will be passed to
        listener *ONLY ONCE* and only if `init` is True!

        One of the reasons for this, is that we cannot easily store the `kwargs`
        into the `_listeners` set, because `dicts` are not hashable.
        """
        assert callable(listener)
        self._listeners.add(WeakMethod(listener))

        if init:
            listener(self.value, **kwargs)

    def unsubscribe(self, listener):
        self._listeners.discard(WeakMethod(listener))

    def notify(self, v):
        for l in self._listeners.copy():
            try:
                l(v)
            except WeakRefLostError:
                # self.unsubscribe(l)
                logging.debug("Not notifying listener which has been dereferenced")
                self._listeners.discard(l)
            except Exception:
                logging.exception("Subscriber %r raised exception when "
                                  "receiving value %s", l, v)


# noinspection PyBroadException
class VigilantAttribute(VigilantAttributeBase):
    """
    A VigilantAttribute represents a value (an object) with:
     * meta-information (min, max, unit, read-only...)
     * observable behaviour (anyone can ask to be notified when the value changes)
    """

    def __init__(self, initval, readonly=False, setter=None, getter=None, max_discard=100, *args, **kwargs):
        """
        readonly (bool): if True, value setter will raise an exception. It's still
            possible to change the value by calling _set() and then notify()
        setter (callable value -> value): function that will be called whenever the value has to
            be changed and returns the new actual value (which might be different
            from what was given).
        getter (callable value -> value): function that will be called whenever the value has to
            be read and returns the current actual value.
        max_discard (int): amount of updates that can be discarded in a row if
                           a new one is already available. 0 to keep (notify)
                           all the messages (dangerous if callback is slower
                           than the generator).
        """
        VigilantAttributeBase.__init__(self, initval, *args, **kwargs)

        self.readonly = readonly
        if setter is None:
            self._setter = self.__default_setter
        else:
            self._setter = WeakMethod(setter) # to avoid cycles

        if getter is not None:
            self._getter = WeakMethod(getter)  # to avoid cycles
        else:
            self._getter = None

        # different from ._listeners for notify() to do different things
        self._remote_listeners = set() # any unique string works

        self._global_name = None # to be filled when registered
        self._ctx = None
        self.pipe = None
        self.max_discard = max_discard

    def __default_setter(self, value):
        return value

    def _getproxystate(self):
        """
        Equivalent to __getstate__() of the proxy version
        """
        proxy_state = Pyro4.core.pyroObjectSerializer(self)[2]
        return (proxy_state, _core.dump_roattributes(self), self.unit,
                self.readonly, self.max_discard)

    def _check(self, value):
        """
        Override to do checking on the value.
        raises exceptions (only)
        """
        pass

    def _get_value(self):
        """The value of this VA"""
        if self._getter is None:
            return self._value
        else:
            try:
                return self._getter()
            except WeakRefLostError:
                return self._value

    # cannot be oneway because we need the exception in case of error
    def _set_value(self, value, must_notify=False, force_write=False):
        """
        Check the value is acceptable, change the VA value and notify listeners
        value: new value to set
        must_notify (bool): if True, will force the notification, even if value
          hasn't changed.
        force_write (bool): if True, will accept to set the value even if the
          VA is readonly. Should only be used by the 'owner' of the VA.
        """
        # TODO need a lock?
        if self.readonly and not force_write:
            raise NotSettableError("Value is read-only")
        prev_value = self._value

        self._check(value) # we allow the setter to even put illegal value, it's the master
        try:
            self._value = self._setter(value)
        except WeakRefLostError:
            self._value = self.__default_setter(value)

        # only notify if the value has changed (or is different from requested)
        try:
            if must_notify:
                pass  # no need to check for more
            elif isinstance(value, numpy.ndarray):
                # For numpy arrays, it's not possible to use !=
                # => just check it's the same object
                if prev_value is not self._value or value is not self._value:
                    must_notify = True
            else:
                if prev_value != self._value or value != self._value:
                    must_notify = True
        except Exception:
            must_notify = True

        if must_notify:
            self.notify(self._value)

    def _del_value(self):
        del self._value

    value = property(_get_value, _set_value, _del_value, "The actual value")

    def _register(self, daemon):
        """ Get the VigilantAttributeBase ready to be shared.

        It gets registered to the Pyro daemon and over 0MQ. It should be called
        only once. Note that you have to call this method to register a VA, a
        simple daemon.register(p) is not enough.

        :param daemon: (Pyro4.Daemon) daemon used to share this object
        """
        daemon.register(self)

        # create a zmq pipe to publish the data
        # Warning: notify() will most likely run in a separate thread, which is
        # not recommended by 0MQ. At least, we should never access it from this
        # thread anymore. To be safe, it might need a pub-sub forwarder proxy inproc
        self._ctx = zmq.Context(1)
        self.pipe = self._ctx.socket(zmq.PUB)
        self.pipe.linger = 1 # don't keep messages more than 1s after close
        # self.pipe.hwm has to be 0 (default), otherwise it drops _new_ values

        uri = daemon.uriFor(self)
        # uri.sockname is the file name of the pyro daemon (with full path)
        self._global_name = uri.sockname + "@" + uri.object
        logging.debug("VA server is registered to send to " + "ipc://" + self._global_name)
        self.pipe.bind("ipc://" + self._global_name)

    def _unregister(self):
        """
        unregister the VA from the daemon and clean up the 0MQ bindings
        """
        daemon = getattr(self, "_pyroDaemon", None)
        if daemon:
            daemon.unregister(self)
        try:
            if self.pipe:
                self.pipe.close()
                self.pipe = None
            if self._ctx: # no ._ctx if exception during init
                self._ctx.term()
                self._ctx = None
        except Exception:
            pass  # we've done our best

    @oneway
    def subscribe(self, listener, init=False, **kwargs):
        """
        listener (string) => uri of listener of zmq
        listener (callable) => method to call (locally)
        """
        # add string to listeners if listener is string
        if isinstance(listener, basestring):
            self._remote_listeners.add(listener)
            if init:
                self.pipe.send_pyobj(self.value)
        else:
            VigilantAttributeBase.subscribe(self, listener, init, **kwargs)

    @oneway
    def unsubscribe(self, listener):
        """
        listener (string) => uri of listener of zmq
        listener (callable) => method to call (locally)
        """
        if isinstance(listener, basestring):
            # remove string from listeners
            self._remote_listeners.discard(listener)
        else:
            VigilantAttributeBase.unsubscribe(self, listener)

    def notify(self, v):
        # publish the data remotely
        if len(self._remote_listeners) > 0:
            self.pipe.send_pyobj(v)

        # publish locally
        VigilantAttributeBase.notify(self, v)

    def __del__(self):
        self._unregister()


# noinspection PyBroadException
class VigilantAttributeProxy(VigilantAttributeBase, Pyro4.Proxy):
    # init is as light as possible to reduce creation overhead in case the
    # object is actually never used
    def __init__(self, uri):
        """
        uri: see Proxy
        """
        Pyro4.Proxy.__init__(self, uri)
        self._global_name = uri.sockname + "@" + uri.object
        VigilantAttributeBase.__init__(self) # TODO setting value=None might not always be valid
        self.max_discard = 100
        self.readonly = False # will be updated in __setstate__

        self._ctx = None
        self._commands = None
        self._thread = None

    @property
    def value(self):
        return self.__getattr__("_get_value")()

    @value.setter
    def value(self, v):
        if self.readonly:
            raise NotSettableError("Value is read-only")
        return self.__getattr__("_set_value")(v)
    # no delete remotely

    # for enumerated VA
    @property
    def choices(self):
        try:
            value = Pyro4.Proxy.__getattr__(self, "_get_choices")()
        except AttributeError:
            # if we let AttributeError, python will look in the super classes,
            # and eventually get a RemoteMethod from the Proxy :-(
            # So return our own NotApplicableError exception
            raise NotApplicableError()
        return value

    # for continuous VA
    @property
    def range(self):
        try:
            value = Pyro4.Proxy.__getattr__(self, "_get_range")()
        except AttributeError:
            raise NotApplicableError()
        return value

    def __getstate__(self):
        # must permit to recreate a proxy in a different container
        proxy_state = Pyro4.Proxy.__getstate__(self)
        # we don't need value, it's always remotely accessed
        return (proxy_state, _core.dump_roattributes(self), self.unit,
                self.readonly, self.max_discard)

    def __setstate__(self, state):
        """
        roattributes (dict string -> value)
        max_discard (int): amount of messages that can be discarded in a row if
                            a new one is already available. 0 to keep (notify)
                            all the messages (dangerous if callback is slower
                            than the generator).
        """
        proxy_state, roattributes, unit, self.readonly, self.max_discard = state
        Pyro4.Proxy.__setstate__(self, proxy_state)
        VigilantAttributeBase.__init__(self, unit=unit)
        _core.load_roattributes(self, roattributes)

        self._global_name = self._pyroUri.sockname + "@" + self._pyroUri.object

        self._ctx = None
        self._commands = None
        self._thread = None

    def _create_thread(self):
        logging.debug("Creating thread for VA %s", self._global_name)
        self._ctx = zmq.Context(1) # apparently 0MQ reuse contexts
        self._commands = self._ctx.socket(zmq.PAIR)
        self._commands.bind("inproc://" + self._global_name)
        self._thread = SubscribeProxyThread(self.notify, self._global_name, self.max_discard, self._ctx)
        self._thread.start()

    def subscribe(self, listener, init=False, **kwargs):
        count_before = len(self._listeners)

        # TODO: when init=True, if already listening, reuse last received value
        VigilantAttributeBase.subscribe(self, listener, init, **kwargs)

        if count_before == 0:
            self._start_listening()

    def _start_listening(self):
        """
        start the remote subscription
        """
        if not self._thread:
            self._create_thread()
        self._commands.send("SUB")
        self._commands.recv() # synchronise

        # send subscription to the actual VA
        # a bit tricky because the underlying method gets created on the fly
        Pyro4.Proxy.__getattr__(self, "subscribe")(self._global_name)

    def unsubscribe(self, listener):
        VigilantAttributeBase.unsubscribe(self, listener)
        if len(self._listeners) == 0:
            self._stop_listening()

    def _stop_listening(self):
        """
        stop the remote subscription
        """
        Pyro4.Proxy.__getattr__(self, "unsubscribe")(self._global_name)
        if self._commands:
            self._commands.send("UNSUB")

    def __del__(self):
        # end the thread (but it will stop as soon as it notices we are gone anyway)
        try:
            if self._thread:
                if self._thread.is_alive():
                    if len(self._listeners):
                        logging.warning("Stopping subscription while there are still subscribers "
                                        "because VA '%s' is going out of context",
                                        self._global_name)
                        Pyro4.Proxy.__getattr__(self, "unsubscribe")(self._global_name)
                    self._commands.send("STOP")
                    self._thread.join(1)
                self._commands.close()
            # self._ctx.term()
        except Exception:
            pass

        try:
            Pyro4.Proxy.__del__(self)
        except Exception:
            pass  # don't be too rough if that fails, it's not big deal anymore


class SubscribeProxyThread(threading.Thread):
    def __init__(self, notifier, uri, max_discard, zmq_ctx):
        """
        notifier (callable): method to call when a new value arrives
        uri (string): unique string to identify the connection
        max_discard (int)
        zmq_ctx (0MQ context): available 0MQ context to use
        """
        threading.Thread.__init__(self, name="zmq for VA " + uri)
        self.daemon = True
        self.uri = uri
        self.max_discard = max_discard
        self._ctx = zmq_ctx
        # don't keep strong reference to notifier so that it can be garbage
        # collected normally and it will let us know then that we can stop
        self.w_notifier = WeakMethod(notifier)

        # create a zmq synchronised channel to receive commands
        self._commands = zmq_ctx.socket(zmq.PAIR)
        self._commands.connect("inproc://" + uri)

        # create a zmq subscription to receive the data
        self.data = zmq_ctx.socket(zmq.SUB)
        self.data.connect("ipc://" + uri)

    def run(self):
        # Process messages for commands and data
        poller = zmq.Poller()
        poller.register(self._commands, zmq.POLLIN)
        poller.register(self.data, zmq.POLLIN)
        discarded = 0
        while True:
            socks = dict(poller.poll())

            # process commands
            if socks.get(self._commands) == zmq.POLLIN:
                message = self._commands.recv()
                if message == "SUB":
                    self.data.setsockopt(zmq.SUBSCRIBE, '')
                    self._commands.send("SUBD")
                elif message == "UNSUB":
                    self.data.setsockopt(zmq.UNSUBSCRIBE, '')
                    # no confirmation (async)
                elif message == "STOP":
                    self._commands.close()
                    self.data.close()
                    return

            # receive data
            if socks.get(self.data) == zmq.POLLIN:
                value = self.data.recv_pyobj()
                # more fresh data already?
                if (
                        self.data.getsockopt(zmq.EVENTS) & zmq.POLLIN and
                        discarded < self.max_discard
                ):
                    discarded += 1
                    continue
                if discarded:
                    logging.debug("VA discarded %d values", discarded)
                discarded = 0

                try:
                    self.w_notifier(value)
                except WeakRefLostError:
                    self._commands.close()
                    self.data.close()
                    return


def unregister_vigilant_attributes(self):
    for _, value in inspect.getmembers(self, lambda x: isinstance(x, VigilantAttribute)):
        value._unregister()


def dump_vigilant_attributes(self):
    """
    return the names and value of all the VAs added to an object (component)
    If a VA is not registered yet, it is registered.
    self (Component): the object (instance of a class).  It must already be
                      registered to a Pyro daemon.
    return (dict string -> value): attribute name -> VigilantAttributeBase
    """
    vas = dict()
    daemon = self._pyroDaemon
    for name, value in inspect.getmembers(self, lambda x: isinstance(x, VigilantAttributeBase)):
        if not hasattr(value, "_pyroDaemon"):
            value._register(daemon)
        vas[name] = value
    return vas


def load_vigilant_attributes(self, vas):
    """
    duplicate the given VAs into the instance.
    useful only for a proxy class
    """
    for name, df in vas.items():
        setattr(self, name, df)


def VASerializer(self):
    """reduce function that automatically replaces Pyro objects by a Proxy"""
    daemon = getattr(self, "_pyroDaemon", None)
    # only return a proxy if the object is a registered pyro object
    if daemon:
        if isinstance(self, ListVA):
            # more advanced proxy for ListVA
            return ListVAProxy, (daemon.uriFor(self),), self._getproxystate()
        else:
            return VigilantAttributeProxy, (daemon.uriFor(self),), self._getproxystate()
    else:
        return self.__reduce__()

Pyro4.Daemon.serializers[VigilantAttribute] = VASerializer


class StringVA(VigilantAttribute):
    """
    A VA which contains a string
    """

    def __init__(self, value="", *args, **kwargs):
        VigilantAttribute.__init__(self, value, *args, **kwargs)

    def _check(self, value):
        if not isinstance(value, basestring):
            raise TypeError("Value '%r' is not a string." % value)


class FloatVA(VigilantAttribute):
    """
    A VA which contains a float
    """

    def __init__(self, value=0.0, *args, **kwargs):
        # make sure the value is a float
        VigilantAttribute.__init__(self, float(value), *args, **kwargs)

    def _check(self, value):
        # can be anything that looks more or less like a float
        if not isinstance(value, numbers.Real):
            raise TypeError("Value '%r' is not a float." % value)


class IntVA(VigilantAttribute):
    """
    A VA which contains a integer
    """

    def __init__(self, value=0, *args, **kwargs):
        VigilantAttribute.__init__(self, value, *args, **kwargs)

    def _check(self, value):
        # we really accept only int, to avoid hiding lose of precision
        if not isinstance(value, int):
            raise TypeError("Value '%r' is not a int." % value)

# ListVA is difficult: not only change of the .value must be detected, but we
# also want to detect change on the list itself. So we have a special
# _NotifyingList which let us know whenever it is updated. It does _not_ work
# recursively.
# In order to support this also over Pyro, we need to provide a special proxy
# which ensures that changes on the list go back to the original object, so
# every modification is converted into a ".value =".


# Helper for the _NotifyingList
def _call_with_notifier(func):
    """ This special function wraps any given method, making sure the
    notifier method is called if the value actually changes.
    """
    def newfunc(self, *args, **kwargs):
        if not hasattr(self, "_notifier"):
            return func(self, *args, **kwargs)

        # This might get expensive with long lists!
        old_val = list(self)
        res = func(self, *args, **kwargs)
        if old_val != self:
            try:
                self._notifier(self)
            except WeakRefLostError:
                logging.debug("No more notifier %s for %s", self._notifier, self._notifier.c, exc_info=1)
                pass
        return res
    return newfunc


class _NotifyingList(list):
    """ This is a subclass of Python's default `list` class for us in ListVA

        The main difference compared to the standard `list` class is that a
        notifier method is called when the value of the object changes.

        This notifier must be a callable and it must be provided at creation
        time.
    """
    def __init__(self, *args, **kwargs):
        """
        notifier (callable): if present, will be called with the list itself
          whenever it's changed.
        """
        if "notifier" in kwargs:
            notifier = kwargs.pop("notifier")
            self._notifier = WeakMethod(notifier)
        else:
            logging.debug("Creating notifying list without notifier")
        list.__init__(self, *args, **kwargs)

    # transform back to a normal list when pickled
    def __reduce__(self):
        return list, (list(self),)

    # We must wrap any method of `list` that can change the value
    __iadd__ = _call_with_notifier(list.__iadd__)
    __imul__ = _call_with_notifier(list.__imul__)
    __setitem__ = _call_with_notifier(list.__setitem__)
    __setslice__ = _call_with_notifier(list.__setslice__)
    __delitem__ = _call_with_notifier(list.__delitem__)
    __delslice__ = _call_with_notifier(list.__delslice__)

    append = _call_with_notifier(list.append)
    extend = _call_with_notifier(list.extend)
    insert = _call_with_notifier(list.insert)
    pop = _call_with_notifier(list.pop)
    remove = _call_with_notifier(list.remove)
    reverse = _call_with_notifier(list.reverse)
    sort = _call_with_notifier(list.sort)


class ListVA(VigilantAttribute):
    """ A VA which contains a list of values
        Modifying the list will cause a notification
    """

    def __init__(self, value=None, *args, **kwargs):
        value = _NotifyingList([] if value is None else value, notifier=self._internal_set_value)
        VigilantAttribute.__init__(self, value, *args, **kwargs)

    def _check(self, value):
        if not isinstance(value, collections.Iterable):
            raise TypeError("Value '%r' is not a list." % value)

    def _internal_set_value(self, value):
        # force notify as comparing to old value will not show any difference
        self._set_value(value, must_notify=True)

    # Redefine the setter, so we can force to listen to internal modifications
    def _set_value(self, value, must_notify=False):
        VigilantAttribute._set_value(self, value, must_notify)
        # TODO: this means that .notify will be called with a simple list,
        # should it be overridden to change to a notifying list? Same for the proxy.
        self._value = _NotifyingList(self._value, notifier=self._internal_set_value)

    value = property(VigilantAttribute._get_value,
                     _set_value,
                     VigilantAttribute._del_value,
                     "The actual value")


class ListVAProxy(VigilantAttributeProxy):
    # VAProxy + listen to modifications inside the list

    @property
    def value(self):
        # Transform a normal list into a notifying one
        raw_list = self.__getattr__("_get_value")()
        # When value change, same as setting the value
        val = _NotifyingList(raw_list, notifier=self.__value_setter)
        return val

    @value.setter
    def value(self, v):
        return self.__value_setter(v)

    # needs to be an explicit method to be able to reference it from the list
    def __value_setter(self, v):
        if self.readonly:
            raise NotSettableError("Value is read-only")
        self.__getattr__("_set_value")(v)


class BooleanVA(VigilantAttribute):
    """
    A VA which contains a boolean
    """

    def __init__(self, value, *args, **kwargs):
        VigilantAttribute.__init__(self, value, *args, **kwargs)

    def _check(self, value):
        # we really accept only boolean, to avoid hiding lose of data
        if not isinstance(value, bool):
            raise TypeError("Value '%r' is not a boolean." % value)


class TupleVA(VigilantAttribute):
    """
    A VA which contains a tuple
    """

    def __init__(self, value=None, *args, **kwargs):
        VigilantAttribute.__init__(self, value, *args, **kwargs)

    def _check(self, value):
        # only accept tuple and None, to avoid hidden data changes, as can occur in lists
        if not isinstance(value, (tuple, NoneType)):
            raise TypeError("Value '%r' is not a tuple." % value)


# TODO maybe should provide a factory that can take a VigilantAttributeBase class and return it
# either Continuous or Enumerated

class Continuous(object):
    """
    Mixin which adds the ability to a VA to specify a min and max.
    It has an attribute range (2-tuple) min, max
    It checks that any value set is min <= val <= max
    """

    def __init__(self, range):
        """
        :param range: (2-tuple)
        """
        self._set_range(range)
        # Indicates whether clipping should occur when the range is changed
        self.clip_on_range = False

    def _get_range(self):
        return self._range

    @property
    def range(self):
        """The range within which the value of the VA can be"""
        return self._get_range()

    # To be called only by the owner of the object
    @range.setter
    def range(self, value):
        self._set_range(value)

    @range.deleter
    def range(self):
        del self._range

    def _set_range(self, new_range):
        """ Set the range after performing some basic constraint checks
        """

        # Range should always have 2 elements
        if len(new_range) != 2:
            raise TypeError("Range '%s' is not a 2-tuple." % str(new_range))

        start, end = new_range
        if not isinstance(start, collections.Iterable):
            start = (start,)
            end = (end,)

        if any(mn > mx for mn, mx in zip(start, end)):
            raise TypeError("Range min %s should be smaller than max %s." %
                            (start, end))

        if hasattr(self, "value"):
            if self.clip_on_range:
                # If the range is changed and the current value is outside of
                # this new range, the value will be adjusted so it falls within
                # this new range.

                self._range = tuple(new_range)
                self.value = self.clip(self.value)
                return
            else:
                if not isinstance(self.value, collections.Iterable):
                    value = (self.value,)
                else:
                    value = self.value

                if not all(mn <= v <= mx for v, mn, mx in zip(value, start, end)):
                    msg = "Current value '%s' is outside of the range %s->%s."
                    raise IndexError(msg % (value, start, end))

        self._range = tuple(new_range)

    @property
    def min(self):
        return self._range[0]

    @property
    def max(self):
        return self._range[1]

    def clip(self, val):
        """ Clip the given value to fit within the range.

        If the range contains vectors of length n, each element of val will be
        clipped separately by position for positions 0..n-1
        return (same type as val): value clipped
        """

        if isinstance(self.min, collections.Iterable):
            clipped = []
            for v, min_v, max_v in zip(val, self.min, self.max):
                clipped.append(max(min(v, max_v), min_v))

            if isinstance(self.value, tuple):
                return tuple(clipped)

            return clipped
        else:
            return max(min(val, self.max), self.min)

    def _check(self, value):
        """
        Raises:
            IndexError if the value is not within the authorised range
        """
        start, end = self.range

        if not isinstance(value, collections.Iterable):
            value = (value,)
            start, end = (start,), (end,)

        if not (len(value) == len(start) == len(end)):
            msg = "Value '%s' is not a %d-tuple."
            raise TypeError(msg % (value, len(start)))

        if not all(mn <= v <= mx for v, mn, mx in zip(value, start, end)):
            msg = "Trying to assign value '%s' outside of the range %s->%s."
            raise IndexError(msg % (value, start, end))


class Enumerated(object):
    """
    Mixin which adds the ability to a VA to specify a set of authorised values.
    It has an attribute choices which is of type set
    It checks that any value set is among choices
    """

    def __init__(self, choices):
        """
        choices (set or dict (value -> str)): all the possible value that can be
         assigned, or if it's a dict all the values that can be assigned and a
         user-readable description of the values.
        """
        self._set_choices(choices)

    def _check(self, value):
        if value not in self._choices:
            raise IndexError("Value %s is not part of possible choices: %s." %
                             (value, ", ".join([str(c) for c in self._choices])))

    def _get_choices(self):
        return self._choices

    @property
    def choices(self):
        return self._get_choices()

    def _set_choices(self, new_choices_raw):
        if isinstance(new_choices_raw, collections.Set):
            new_choices = frozenset(new_choices_raw)
        elif isinstance(new_choices_raw, dict):
            new_choices = dict(new_choices_raw)
        else:
            raise TypeError("Choices %s is not a set." % str(new_choices_raw))
        if hasattr(self, "value"):
            if self.value not in new_choices:
                raise IndexError("Current value %s is not part of possible choices: %s." %
                                 (self.value, ", ".join([str(c) for c in new_choices])))
        self._choices = new_choices

    @choices.setter
    def choices(self, value):
        self._set_choices(value)

    @choices.deleter
    def choices(self):
        del self._choices


class VAEnumerated(VigilantAttribute, Enumerated):
    """
    VigilantAttribute which contains any kind of values from a given set.
    """

    def __init__(self, value, choices, **kwargs):
        """
        choices (set or dict (value -> str)): all the possible value that can be
         assigned, or if it's a dict all the values that can be assigned and a
         user-readable description of the values.
        """
        Enumerated.__init__(self, choices)
        VigilantAttribute.__init__(self, value, **kwargs)

    def _check(self, value):
        Enumerated._check(self, value)
        VigilantAttribute._check(self, value)


class FloatContinuous(FloatVA, Continuous):
    """
    A simple class which is both float and continuous
    """
    def __init__(self, value, range, **kwargs):
        Continuous.__init__(self, range)
        FloatVA.__init__(self, value, **kwargs)

    def _check(self, value):
        Continuous._check(self, value)
        FloatVA._check(self, value)


class IntContinuous(IntVA, Continuous):
    """
    A simple class which is both int and continuous
    """
    def __init__(self, value, range, **kwargs):
        Continuous.__init__(self, range)
        IntVA.__init__(self, value, **kwargs)

    def _check(self, value):
        Continuous._check(self, value)
        IntVA._check(self, value)


class StringEnumerated(StringVA, Enumerated):
    """
    A simple class which is both string and Enumerated
    """
    def __init__(self, value, choices, **kwargs):
        Enumerated.__init__(self, choices)
        StringVA.__init__(self, value, **kwargs)

    def _check(self, value):
        Enumerated._check(self, value)
        StringVA._check(self, value)


class FloatEnumerated(FloatVA, Enumerated):
    """
    A simple class which is both floating and enumerated
    """
    def __init__(self, value, choices, **kwargs):
        Enumerated.__init__(self, choices)
        FloatVA.__init__(self, value, **kwargs)

    def _check(self, value):
        Enumerated._check(self, value)
        FloatVA._check(self, value)

# TODO: needed? For now, there is just something similar in the CLI
#     def _set_value(self, value, must_notify=False):
#         if value not in self._choices:
#             # allow some room for floating point error
#             closest = util.find_closest(value, self._choices)
#             if util.almost_equal(value, closest):
#                 logging.info("Changing requested value %g to closest choice %g",
#                              value, closest)
#                 value = closest
#         super(FloatEnumerated, self)._set_value(value, must_notify)


class IntEnumerated(IntVA, Enumerated):
    """
    A simple class which is both int and enumerated
    """
    def __init__(self, value, choices, **kwargs):
        Enumerated.__init__(self, choices)
        IntVA.__init__(self, value, **kwargs)

    def _check(self, value):
        Enumerated._check(self, value)
        IntVA._check(self, value)


class MultiSpeedVA(VigilantAttribute, Continuous):
    """
    A class to define speed (m/s) for several axes
    It's especially made for Actuator.speed: the value is a dict name => float
    Also the speed must be >0
    """
    def __init__(self, value, range, unit="m/s", *args, **kwargs):
        Continuous.__init__(self, range)
        assert(range[0] >= 0)
        VigilantAttribute.__init__(self, value, unit=unit, *args, **kwargs)

    # TODO detect whenever a value of the dict is changed
    def _check(self, value):
        # a dict
        if not isinstance(value, dict):
            raise TypeError("Value '%s' is not a dict." % str(value))
        for axis, v in value.items():
            # It has to be within the range, but also > 0
            if v <= 0 or v < self._range[0] or v > self._range[1]:
                raise IndexError(
                    "Trying to assign axis '%s' value '%s' outside of the range %s->%s." %
                    (axis, v, self._range[0], self._range[1]))


class TupleContinuous(VigilantAttribute, Continuous):
    """
    VigilantAttribute which contains tuple of fixed length and has all the
    elements of the same type.
    It's allowed to request any value within min and max (but might also have
    additional constraints).
    The length of the original value determines the allowed tuple length.
    """

    def __init__(self, value, range, unit="", cls=None, **kwargs):
        """
        range (2 x tuple): minimum and maximum size for each dimension
        cls (class or list of classes): classes allowed for each element of the tuple
          default to the same class as the first element
        """
        if not isinstance(value, tuple):
            raise ValueError("value (%s) is not a tuple" % (value,))
        self._cls = cls or value[0].__class__
        Continuous.__init__(self, range)
        VigilantAttribute.__init__(self, value, unit=unit, **kwargs)

    def _set_value(self, value):
        # force tuple
        value = tuple(value)
        VigilantAttribute._set_value(self, value)
    # need to overwrite the whole property
    value = property(VigilantAttribute._get_value, _set_value, VigilantAttribute._del_value,
                     "The actual value")

    def _check(self, value):
        if not all([isinstance(v, self._cls) for v in value]):
            msg = "Value '%s' must be a tuple only consisting of types %s."
            raise TypeError(msg % (value, self._cls))
        Continuous._check(self, value)


class ResolutionVA(TupleContinuous):
    # old name for TupleContinuous, when it was fixed to len == 2 and cls == int
    # and default unit == "px"
    def __init__(self, value, rng, unit="px", cls=None, **kwargs):
        cls = cls or (int, long)
        TupleContinuous.__init__(self, value, rng, unit=unit, cls=cls, **kwargs)
