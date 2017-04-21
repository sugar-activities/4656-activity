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

import os
import logging

from tornado import httpserver
from tornado import ioloop
from tornado import web
from tornado import websocket

from gi.repository import GLib

import utils
import tempfile
import base64
import json
import StringIO

import cairo
from sugar3.graphics.icon import _IconBuffer


class DatastoreHandler(web.StaticFileHandler):

    def set_extra_headers(self, path):
        """For subclass to add extra headers to the response"""
        self.set_header("Content-Type", 'application/journal')
        self._path = path


class IconHandler(web.RequestHandler):

    def initialize(self, path):
        self._path = path

    def get(self, *args, **kwargs):
        logging.error('requested %s', (args))
        image_name = args[0]
        [icon_name, stroke_color, fill_color] = image_name.split('_')
        icon_name = str(icon_name) + '.svg'
        logging.error('icon_name %s stroke %s fill %s',
            icon_name, stroke_color, fill_color)

        icon_buffer = _IconBuffer()
        icon_buffer.file_name = os.path.join(self._path, 'images', icon_name)
        icon_buffer.stroke_color = '#%s' % str(stroke_color)
        icon_buffer.fill_color = '#%s' % str(fill_color)
        icon_buffer.width = 50
        icon_buffer.height = 50
        icon_surface = icon_buffer.get_surface()
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, icon_buffer.width,
            icon_buffer.height)
        context = cairo.Context(surface)
        context.set_source_rgba(1, 1, 1, 1)
        context.rectangle(0, 0, icon_buffer.width, icon_buffer.height)
        context.fill()
        context.set_source_surface(icon_surface, 0, 0)
        context.paint()
        out = StringIO.StringIO()
        surface.write_to_png(out)
        self.write(out.getvalue())
        self.finish()

    def write(self, chunk):
        """
        Overwrited to avoid write the content as utf8
        """
        if self._finished:
            raise RuntimeError("Cannot write() after finish().  May be caused "
                               "by using async operations without the "
                               "@asynchronous decorator.")
        self._write_buffer.append(chunk)


class JournalWebSocketHandler(websocket.WebSocketHandler):

    def initialize(self, instance_path, journal_manager):
        self._instance_path = instance_path
        self._jm = journal_manager
        self._jm.connect('updated', self.__journal_manager_updated_cb)

    def __journal_manager_updated_cb(self, jm):
        logging.error('ON JournalWebSocketHandler jm updated')
        try:
            f = open(os.path.join(self._instance_path, 'selected.json'))
            logging.error(os.path.join(self._instance_path, 'selected.json'))
            json = f.read()
            f.close()
            logging.error(json)
            self.write_message(json)
        except:
            logging.error('Exception sending websocket msg')

    def open(self):
        logging.error("WebSocket opened")

    def on_message(self, message):
        logging.error('RECEIVED MSG: %s', message)
        message_data = json.loads(message)
        if message_data['type_message'] == 'DOWNLOADED':
            message = message_data['message']
            object_id = message['object_id']
            name = message['from']
            icon = message['icon']
            logging.error('OBJECT %s WAS DOWNLOADED SUCCESSFULLY', object_id)
            GLib.idle_add(self._jm.add_downloader, object_id, name, icon)
        else:
            self.write_message(u"You said: " + message)

    def on_close(self):
        logging.error("WebSocket closed")


class WebSocketUploadHandler(websocket.WebSocketHandler):

    def initialize(self, instance_path, journal_manager):
        self._instance_path = instance_path
        self._jm = journal_manager

    def open(self):
        self._tmp_file = tempfile.NamedTemporaryFile(
            mode='r+', dir=self._instance_path)

    def on_message(self, message):
        self._tmp_file.write(message)
        self._tmp_file.flush()
        self.write_message('NEXT')

    def on_close(self):
        # save to the journal
        # decode the file
        self._decoded_tmp_file = tempfile.NamedTemporaryFile(
            mode='r+', dir=self._instance_path)
        self._tmp_file.seek(0)
        base64.decode(self._tmp_file, self._decoded_tmp_file)
        self._decoded_tmp_file.flush()

        metadata, preview_data, file_path = \
            utils.unpackage_ds_object(self._decoded_tmp_file.name)
        logging.error('METADATA %s', metadata)

        GLib.idle_add(self._jm.create_object, file_path, metadata,
                      preview_data)
        self._tmp_file.close()
        self._decoded_tmp_file.close()


def run_server(activity_path, activity_root, jm, port):

    from threading import Thread
    io_loop = ioloop.IOLoop.instance()

    static_path = os.path.join(activity_path, 'web')
    instance_path = os.path.join(activity_root, 'instance')

    application = web.Application(
        [
            (r"/web/(.*)", web.StaticFileHandler, {"path": static_path}),
            (r"/icon/(.*)", IconHandler, {"path": static_path}),
            (r"/datastore/(.*)", DatastoreHandler, {"path": instance_path}),
            (r"/websocket", JournalWebSocketHandler,
                {"instance_path": instance_path, "journal_manager": jm}),
            (r"/websocket/upload", WebSocketUploadHandler,
                {"instance_path": instance_path, "journal_manager": jm})
        ])
    http_server = httpserver.HTTPServer(application)
    http_server.listen(port)
    tornado_looop = Thread(target=io_loop.start)
    tornado_looop.setDaemon(True)
    tornado_looop.start()
    logging.error('SERVER STARTED')
