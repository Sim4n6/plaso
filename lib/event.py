#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright 2012 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This file contains the EventObject and all implementations of it.

This file contains the definition for the EventObject and EventContainer,
which are core components of the storage mechanism of plaso.

"""
import heapq
import re

from plaso.lib import errors
from plaso.lib import eventdata
from plaso.lib import timelib
from plaso.lib import utils
from plaso.proto import plaso_storage_pb2
from plaso.proto import transmission_pb2

# Regular expression used for attribute filtering
UPPER_CASE = re.compile('[A-Z]')


class EventContainer(object):
  """The EventContainer serves as a basic storage mechansim for plaso.

  An event container is a simple placeholder that is used to store
  EventObjects. It can also hold other EventContainer objects and basic common
  attributes among the EventObjects that are stored within it.

  An example of this scheme is:
    One container stores all logs from host A. That container therefore stores
    the hostname attribute.
    Then for each file that gets parsed a new container is created, which
    holds the common attributes for that file.

  That way the EventObject does not need to store all these attributes, only
  those that differ between different containers.

  The container also stores two timestamps, one for the first timestamp of
  all EventObjects that it contains, and the second one for the last one.
  That makes timebased filtering easier, since filtering can discard whole
  containers if they are outside the scope instead of going through each
  and every event.
  """

  # Define needed attributes
  events = None
  containers = None
  parent_container = None
  first_timestamp = None
  last_timestamp = None
  attributes = None

  def __init__(self):
    """Initializes the event container."""
    # A placeholder for all EventObjects directly stored.
    self.events = []

    # A placeholder for all EventContainer directly stored.
    self.containers = []

    self.first_timestamp = 0
    self.last_timestamp = 0

    self.attributes = {}

  def __setattr__(self, attr, value):
    """Sets the value to either the default or the attribute store."""
    try:
      object.__getattribute__(self, attr)
      object.__setattr__(self, attr, value)
    except AttributeError:
      self.attributes.__setitem__(attr, value)

  def __getattr__(self, attr):
    """Return attribute value from either attribute store.

    Args:
      attr: The attribute name

    Returns:
      The attribute value if one is found.

    Raise:
      AttributeError: If the object does not have the attribute
                      in either store.
    """
    try:
      return object.__getattribute__(self, attr)
    except AttributeError:
      pass

    # Try getting the attributes from the other attribute store.
    try:
      return self.GetValue(attr)
    except AttributeError:
      raise AttributeError('%s\' object has no attribute \'%s\'.' % (
          self.__class__.__name__, attr))

  def __len__(self):
    """Retrieves the number of items in the containter and its sub items."""
    counter = len(self.events)
    for container in self.containers:
      counter += len(container)

    return counter

  def GetValue(self, attr):
    """Determine if an attribute is set in container or in parent containers.

    Since attributes can be set either at the container level or at the
    event level, we need to provide a mechanism to traverse the tree and
    determine if the attribute has been set or not.

    Args:
      attr: The name of the attribute that needs to be checked.

    Returns:
      The attribute value if it exists, otherwise an exception is raised.

    Raises:
      AttributeError: if the attribute is not defined in either the container
                      itself nor in any parent containers.
    """
    if attr in self.attributes:
      return self.attributes.__getitem__(attr)

    if self.parent_container:
      return self.parent_container.GetValue(attr)

    raise AttributeError("'%s' object has no attribute '%s'." % (
        self.__class__.__name__, attr))

  def GetAttributes(self):
    """Return a set of all defined attributes.

    This returns attributes defined in the object that do not fall
    under the following criteria:
      + Starts with _
      + Starts with an upper case letter.

    Returns:
      A set that contains all the attributes that are either stored
      in the attribute store or inside the attribute store of any
      of the parent containers.
    """
    res = set(self.attributes.keys())

    if self.parent_container:
      res |= self.parent_container.GetAttributes()

    return res

  def __iter__(self):
    """An iterator that returns alls EventObjects stored in the containers."""
    all_events = []

    for event in self.events:
      heapq.heappush(all_events, (event.timestamp, event))
    for container in self.containers:
      for event in container:
        heapq.heappush(all_events, (event.timestamp, event))

    for _ in range(len(all_events)):
      yield heapq.heappop(all_events)[1]

  def Append(self, item):
    """Appends an event container or object to the container.

    Args:
      item: The event containter (EventContainer) or object (EventObject)
            to append.

    Raises:
      errors.NotAnEventContainerOrObject: When an object is passed to the
      function that is not an EventObject or an EventContainer.
    """
    try:
     if isinstance(item, EventObject):
       self._Append(item, self.events, item.timestamp)
       return
     elif isinstance(item, EventContainer):
       self._Append(item, self.containers, item.first_timestamp,
                    item.last_timestamp)
       return
    except (AttributeError, TypeError):
      pass

    raise errors.NotAnEventContainerOrObject('Unable to determine the object.')

  def _Append(self, item, storage, timestamp_first, timestamp_last=None):
    """Append objects to container while checking timestamps."""
    item.parent_container = self
    storage.append(item)

    if not timestamp_last:
      timestamp_last = timestamp_first

    if not self.last_timestamp:
      self.last_timestamp = timestamp_last

    if not self.first_timestamp:
      self.first_timestamp = timestamp_first

    if timestamp_last > self.last_timestamp:
      self.last_timestamp = timestamp_last

    if timestamp_first < self.first_timestamp:
      self.first_timestamp = timestamp_first


class EventObject(object):
  """An event object is the main datastore for an event in plaso.

  The framework is designed to parse files and create an event
  from every single record, line or key extracted from the file.

  An EventContainer is the main data store for that event, however
  the container only contains information about common atttributes
  to the event and information about all the EventObjects that are
  associated to that event. The EventObject is more tailored to the
  content of the parsed data and it will contain the actual data
  portion of the Event.

  This class defines the high level interface of EventObject.
  Before creating an EventObject a class needs to be implemented
  that inherits from EventObject and implements the functions in it.

  The EventObject is then used by output processing for saving
  in other forms, such as a protobuff, AFF4 container, CSV files,
  databases, etc.

  The goal of the EventObject is to provide a easily extensible
  data storage of each events internally in the tool.

  The main EventObject only exposes those functions that the
  implementations need to implement. The functions that are needed
  simply provide information about the event, or describe the
  attributes that are necessary. How they are assembled is totally
  up to the implementation.

  All required attributes of the EventObject are passed to the
  constructor of the object while the optional ones are set
  using the method SetValue(attribute, value).
  """

  # Lists of the mappings between the source short values of the event object
  # and those used in the protobuf.
  _SOURCE_SHORT_FROM_PROTO_MAP = {}
  _SOURCE_SHORT_TO_PROTO_MAP = {}
  for value in plaso_storage_pb2.EventObject.DESCRIPTOR.enum_types_by_name[
      'SourceShort'].values:
    _SOURCE_SHORT_FROM_PROTO_MAP[value.number] = value.name
    _SOURCE_SHORT_TO_PROTO_MAP[value.name] = value.number
  _SOURCE_SHORT_FROM_PROTO_MAP.setdefault(6)
  _SOURCE_SHORT_TO_PROTO_MAP.setdefault('LOG')

  parent_container = None
  attributes = None

  def __init__(self):
    """Initializes the event object."""
    self.attributes = {}

  def __setattr__(self, attr, value):
    """Sets the value to either the default or the attribute store."""
    try:
      object.__getattribute__(self, attr)
      object.__setattr__(self, attr, value)
    except AttributeError:
      self.attributes.__setitem__(attr, value)

  def __getattr__(self, attr):
    """Determine if attribute is set within the event or in a container."""
    try:
      return object.__getattribute__(self, attr)
    except AttributeError:
      pass

    # Check the attribute store.
    try:
      if attr in self.attributes:
        return self.attributes.__getitem__(attr)
    except TypeError as e:
      raise AttributeError('[Event] %s', e)

    # Check the parent.
    if self.parent_container:
      try:
        return self.parent_container.GetValue(attr)
      except AttributeError:
        raise AttributeError('%s\' object has no attribute \'%s\'.' % (
            self.__class__.__name__, attr))

    raise AttributeError('Attribute [%s] not defined' % attr)

  def GetAttributes(self):
    """Return a list of all defined attributes."""
    res = set(self.attributes.keys())

    if self.parent_container:
      res |= self.parent_container.GetAttributes()

    return res

  def __str__(self):
    """Print a human readable string from the EventObject."""

    message, _ = eventdata.EventFormatterManager.GetMessageStrings(self)
    if not message:
      return 'Unable to print event, no formatter defined.'

    time = 0
    short = u''
    s_long = u''

    __pychecker__ = ('missingattrs=timestamp,source_short,'
                     'source_long')
    try:
      time = self.timestamp
    except AttributeError:
      pass

    try:
      short = self.source_short
    except AttributeError:
      pass
    try:
      s_long = self.source_long
    except AttributeError:
      pass

    return u'[{0}] {1}/{2} - {3}'.format(time, short, s_long, message)

  def _AttributeFromProto(self, proto):
    """Unserializes an event object attribute from a protobuf.

    Args:
      proto: The attribute protobuf (plaso_storage_pb2.Attribute).

    Returns:
      A list containing the name and value of the attribute.

    Raises:
      RuntimeError: when the protobuf is not of type:
                    plaso_storage_pb2.Attribute or if the attribute
                    cannot be unserialized.
    """
    key = u''
    try:
      if proto.HasField('key'):
        key = proto.key
    except ValueError:
      pass

    if not isinstance(proto, (
        plaso_storage_pb2.Attribute, plaso_storage_pb2.Value)):
      raise RuntimeError("Unsupported proto")

    if proto.HasField('string'):
      return key, proto.string

    elif proto.HasField('integer'):
      return key, proto.integer

    elif proto.HasField('boolean'):
      return key, proto.boolean

    elif proto.HasField('dict'):
      value = {}

      for proto_dict in proto.dict.attributes:
        dict_key, dict_value = self._AttributeFromProto(proto_dict)
        value[dict_key] = dict_value
      return key, value

    elif proto.HasField('array'):
      value = []

      for proto_array in proto.array.values:
        _, list_value = self._AttributeFromProto(proto_array)
        value.append(list_value)
      return key, value

    elif proto.HasField('data'):
      return key, proto.data

    # TODO: deal with float.
    else:
      raise RuntimeError("Unsupported proto attribute type.")

  def FromProto(self, proto):
    """Unserializes the event object from a protobuf.

    Args:
      proto: The protobuf (plaso_storage_pb2.EventObject).

    Raises:
      RuntimeError: when the protobuf is not of type:
                    plaso_storage_pb2.EventObject or when an unsupported
                    attribute value type is encountered
    """
    if not isinstance(proto, plaso_storage_pb2.EventObject):
      raise RuntimeError("Unsupported proto")

    for proto_attribute, value in proto.ListFields():
      if proto_attribute.name == 'source_short':
        self.attributes.__setitem__(
            'source_short', self._SOURCE_SHORT_FROM_PROTO_MAP[value])

      elif proto_attribute.name == 'pathspec':
        self.attributes.__setitem__(
            'pathspec', proto.pathspec.SerializeToString())

      elif proto_attribute.name == 'attributes':
        continue
      else:
        # Register the attribute correctly.
        self.attributes.__setitem__(proto_attribute.name, value)

    # Make sure the old attributes are removed.
    self.attributes.update(dict(self._AttributeFromProto(a) for a in
                                proto.attributes))

  def ToProtoString(self):
    """Serialize an event object into a string value."""
    proto = self.ToProto()

    return proto.SerializeToString()

  def FromProtoString(self, proto_string):
    """Unserializes the event object from a serialized protobuf."""
    proto = plaso_storage_pb2.EventObject()
    proto.ParseFromString(proto_string)
    self.FromProto(proto)

  def _AttributeToProto(self, proto, name, value):
    """Serializes an event object attribute to a protobuf.

    The attribute in an event object can store almost any arbitrary data, so
    the corresponding protobuf storage must deal with the various data types.
    This method identifies the data type and assigns it properly to the
    attribute protobuf.

    Args:
      proto: The attribute protobuf (plaso_storage_pb2.Attribute).
      name: The name of the attribute.
      value: The value of the attribute.
    """
    if name:
      proto.key = name

    if isinstance(value, (str, unicode)):
      proto.string = utils.GetUnicodeString(value)

    elif isinstance(value, (int, long)):
      # TODO: add some bounds checking.
      proto.integer = value

    elif isinstance(value, bool):
      proto.boolean = value

    elif isinstance(value, dict):
      proto_dict = plaso_storage_pb2.Dict()

      for dict_key, dict_value in value.items():
        sub_proto = proto_dict.attributes.add()
        self._AttributeToProto(sub_proto, dict_key, dict_value)
      proto.dict.MergeFrom(proto_dict)

    elif isinstance(value, (list, tuple)):
      proto_array = plaso_storage_pb2.Array()

      for list_value in value:
        sub_proto = proto_array.values.add()
        self._AttributeToProto(sub_proto, '', list_value)
      proto.array.MergeFrom(proto_array)

    # TODO: deal with float.
    else:
      proto.data = value

  def ToProto(self):
    """Serializes the event object into a protobuf.

    Returns:
      A protobuf (plaso_storage_pb2.EventObject).
    """
    proto = plaso_storage_pb2.EventObject()

    for attribute_name in self.GetAttributes():
      if attribute_name == 'source_short':
        __pychecker__ = ('missingattrs=source_short,pathspec')
        proto.source_short = self._SOURCE_SHORT_TO_PROTO_MAP[self.source_short]

      elif attribute_name == 'pathspec':
        proto.pathspec.MergeFromString(self.pathspec)

      elif hasattr(proto, attribute_name):
        attribute_value = getattr(self, attribute_name)

        if isinstance(attribute_value, (str, unicode)):
          attribute_value = utils.GetUnicodeString(attribute_value)
        setattr(proto, attribute_name, attribute_value)

      else:
        attribute_value = getattr(self, attribute_name)

        # TODO: deal with float.
        # Serialize the attribute value only if it is an integer type
        # (int or long) or if it has a value.
        # TODO: fix logic.
        if isinstance(attribute_value, (bool, int, long)) or attribute_value:
          proto_attribute = proto.attributes.add()
          self._AttributeToProto(
              proto_attribute, attribute_name, attribute_value)

    return proto


class FatDateTimeEvent(EventObject):
  """Convenience class for a FAT date time-based event."""

  def __init__(self, fat_date_time, usage):
    """Initializes a FAT date time-based event object.

    Args:
      fat_dat_time: The FAT date time value.
      usage: The description of the usage of the time value.
    """
    super(FatDateTimeEvent, self).__init__()
    self.timestamp = timelib.Timestamp.FromFatDateTime(fat_date_time)
    self.timestamp_desc = usage


class FiletimeEvent(EventObject):
  """Convenience class for a FILETIME timestamp-based event."""

  def __init__(self, filetime, usage):
    """Initializes a FILETIME timestamp-based event object.

    Args:
      filetime: The FILETIME timestamp value.
      usage: The description of the usage of the time value.
    """
    super(FiletimeEvent, self).__init__()
    self.timestamp = timelib.Timestamp.FromFiletime(filetime)
    self.timestamp_desc = usage


class PosixTimeEvent(EventObject):
  """Convenience class for a POSIX time-based event."""

  def __init__(self, posix_time, usage):
    """Initializes a POSIX times-based event object.

    Args:
      posix_time: The POSIX time value.
      usage: The description of the usage of the time value.
    """
    super(PosixTimeEvent, self).__init__()
    self.timestamp = timelib.Timestamp.FromPosixTime(posix_time)
    self.timestamp_desc = usage


class RegistryEvent(EventObject):
  """Convenience class for a Windows Registry-based event."""

  # Add few class variables so they don't get defined as special attributes.
  keyvalue_dict = u''
  source_append = u''

  def __init__(self, key, value_dict, timestamp=None, usage=None):
    """Initializes a Windows registry event.

    Args:
      key: Name of the registry key being parsed.
      value_dict: The interpreted value of the key, stored as a dictionary.
      timestamp: Optional timestamp time value. The timestamp contains the
                 number of microseconds since Jan 1, 1970 00:00:00 UTC.
      usage: The description of the usage of the time value.
    """
    super(RegistryEvent, self).__init__()
    self.source_short = 'REG'
    if key:
      self.keyname = key
    self.keyvalue_dict = value_dict
    self.timestamp = timestamp
    self.timestamp_desc = usage or 'Last Written'
    self.regvalue = value_dict


class TextEvent(EventObject):
  """Convenience class for a text log file-based event."""

  def __init__(self, timestamp, attributes, source):
    """Initializes a text event.

    Args:
      timestamp: The timestamp time value. The timestamp contains the
                 number of microseconds since Jan 1, 1970 00:00:00 UTC.
      attributes: A dict that contains the events attributes.
      source: The source_long description of the event.
    """
    super(TextEvent, self).__init__()
    self.timestamp = timestamp
    self.timestamp_desc = 'Entry Written'
    self.source_short = 'LOG'
    self.source_long = source
    for name, value in attributes.items():
      # TODO: Revisit this constraints and see if we can implement
      # it using a more sane solution.
      if isinstance(value, (str, unicode)) and not value:
        continue
      self.attributes.__setitem__(name, value)


class SQLiteEvent(EventObject):
  """Convenience class for a SQLite-based event."""

  def __init__(self, timestamp, usage, source_short, source_long):
    """Initializes the SQLite-based event.

    Args:
      timestamp: The timestamp time value. The timestamp contains the
                 number of microseconds since Jan 1, 1970 00:00:00 UTC.
      usage: The description of the usage of the time value.
      source_short: A string containing a long description of the source.
      source_long: A string containing a short description of the source.
    """
    super(SQLiteEvent, self).__init__()
    self.timestamp = timestamp
    self.timestamp_desc = usage
    self.source_short = source_short
    self.source_long = source_long

