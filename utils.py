
# Copyright 2013 Agustin Zubiaga <aguz@sugarlabs.org>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

from gi.repository import GObject
import base64
import os
import json
import dbus
from zipfile import ZipFile
import logging
from threading import Thread

import websocket
import tempfile

from sugar3 import profile

CHUNK_SIZE = 2048


class Uploader(GObject.GObject):

    __gsignals__ = {'uploaded': (GObject.SignalFlags.RUN_FIRST, None, ([]))}

    def __init__(self, file_path, url):
        GObject.GObject.__init__(self)
        logging.error('websocket url %s', url)
        # base64 encode the file
        self._file = tempfile.TemporaryFile(mode='r+')
        base64.encode(open(file_path, 'r'), self._file)
        self._file.seek(0)

        self._ws = websocket.WebSocketApp(url,
                                          on_open=self._on_open,
                                          on_message=self._on_message,
                                          on_error=self._on_error,
                                          on_close=self._on_close)
        self._chunk = str(self._file.read(CHUNK_SIZE))

    def start(self):
        upload_looop = Thread(target=self._ws.run_forever)
        upload_looop.setDaemon(True)
        upload_looop.start()

    def _on_open(self, ws):
        if self._chunk != '':
            self._ws.send(self._chunk)
        else:
            self._ws.close()

    def _on_message(self, ws, message):
        self._chunk = self._file.read(CHUNK_SIZE)
        if self._chunk != '':
            self._ws.send(self._chunk)
        else:
            self._ws.close()

    def _on_error(self, ws, error):
        #self._ws.send(self._chunk)
        pass

    def _on_close(self, ws):
        self._file.close()
        GObject.idle_add(self.emit, 'uploaded')


class Messanger(GObject.GObject):

    __gsignals__ = {'sent': (GObject.SignalFlags.RUN_FIRST, None, ([str]))}

    def __init__(self, url):
        GObject.GObject.__init__(self)
        logging.error('websocket url %s', url)
        self._ws = websocket.WebSocketApp(url,
                                          on_open=self._on_open,
                                          on_message=self._on_message,
                                          on_error=self._on_error)

    def send_message(self, type_message, message):
        self._message_data = {'type_message': type_message, 'message': message}
        message_looop = Thread(target=self._ws.run_forever)
        message_looop.setDaemon(True)
        message_looop.start()

    def _on_open(self, ws):
        self._ws.send(json.dumps(self._message_data))

    def _on_message(self, ws, message):
        message_data = json.loads(message)
        GObject.idle_add(self.emit, 'sent', message_data)

    def _on_error(self, ws, error):
        pass


def get_user_data():
    """
    Create this structure:
    {"from": "Walter Bender", "icon": ["#FFC169", "#FF2B34"]}
    used to identify the owner of a shared object
    is compatible with how the comments are saved in
    http://wiki.sugarlabs.org/go/Features/Comment_box_in_journal_detail_view
    """
    xo_color = profile.get_color()
    data = {}
    data['from'] = profile.get_nick_name()
    data['icon'] = [xo_color.get_stroke_color(), xo_color.get_fill_color()]
    return data


def package_ds_object(dsobj, destination_path):
    """
    Creates a zipped file with the file associated to a journal object,
    the preview and the metadata
    """
    object_id = dsobj.object_id
    logging.error('id %s', object_id)
    preview_path = None

    logging.error('before preview')
    if 'preview' in dsobj.metadata:
        # TODO: copied from expandedentry.py
        # is needed because record is saving the preview encoded
        if dsobj.metadata['preview'][1:4] == 'PNG':
            preview = dsobj.metadata['preview']
        else:
            # TODO: We are close to be able to drop this.
            preview = base64.b64decode(dsobj.metadata['preview'])

        preview_path = os.path.join(destination_path,
                                    'preview_id_' + object_id)
        preview_file = open(preview_path, 'w')
        preview_file.write(preview)
        preview_file.close()

    logging.error('before metadata')
    # create file with the metadata
    metadata_path = os.path.join(destination_path,
                                 'metadata_id_' + object_id)
    metadata_file = open(metadata_path, 'w')
    metadata = {}
    for key in dsobj.metadata.keys():
        if key not in ('object_id', 'preview', 'progress'):
            metadata[key] = dsobj.metadata[key]
    metadata['original_object_id'] = dsobj.object_id

    metadata_file.write(json.dumps(metadata))
    metadata_file.close()

    logging.error('before create zip')

    # create a zip fileincluding metadata and preview
    # to be read from the web server
    file_path = os.path.join(destination_path, 'id_' + object_id + '.journal')

    with ZipFile(file_path, 'w') as myzip:
        if preview_path is not None:
            myzip.write(preview_path, 'preview')
        myzip.write(metadata_path, 'metadata')
        myzip.write(dsobj.file_path, 'data')
    return file_path


def unpackage_ds_object(origin_path):
    """
    Receive a path of a zipped file, unzip it, and save the data,
    preview and metadata on a journal object
    """
    tmp_path = os.path.dirname(origin_path)
    with ZipFile(origin_path) as zipped:
        metadata = json.loads(zipped.read('metadata'))
        preview_data = zipped.read('preview')
        zipped.extract('data', tmp_path)

    return metadata, preview_data, os.path.join(tmp_path, 'data')
