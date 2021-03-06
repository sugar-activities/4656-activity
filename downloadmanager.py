# Copyright (C) 2007, One Laptop Per Child
# Copyright (C) 2009, Tomeu Vizoso, Lucian Branescu
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
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
from gettext import gettext as _
import tempfile
import dbus

from gi.repository import Gtk
from gi.repository import WebKit

from sugar3.datastore import datastore
from sugar3 import profile
from sugar3 import mime
from sugar3.graphics.alert import Alert, TimeoutAlert
from sugar3.graphics.icon import Icon
from sugar3.activity import activity

import utils

DS_DBUS_SERVICE = 'org.laptop.sugar.DataStore'
DS_DBUS_INTERFACE = 'org.laptop.sugar.DataStore'
DS_DBUS_PATH = '/org/laptop/sugar/DataStore'

_active_downloads = []
_dest_to_window = {}

SPACE_THRESHOLD = 52428800


def format_float(f):
    return "%0.2f" % f


def can_quit():
    return len(_active_downloads) == 0


def num_downloads():
    return len(_active_downloads)


def remove_all_downloads():
    for download in _active_downloads:
        download.cancel()
        if download.dl_jobject is not None:
            datastore.delete(download.dl_jobject.object_id)
        download.cleanup()


class Download(object):
    def __init__(self, download, browser):
        self._download = download
        self._activity = browser.get_toplevel()
        self._source = download.get_uri()

        self._download.connect('notify::status', self.__state_change_cb)
        self._download.connect('error', self.__error_cb)

        self.datastore_deleted_handler = None

        self.dl_jobject = None
        self._object_id = None
        self._last_update_time = 0
        self._last_update_percent = 0
        self._stop_alert = None

        # figure out download URI
        self.temp_path = os.path.join(activity.get_activity_root(), 'instance')
        if not os.path.exists(self.temp_path):
            os.makedirs(self.temp_path)

        fd, self._dest_path = tempfile.mkstemp(
            dir=self.temp_path,
            suffix=download.get_suggested_filename(),
            prefix='tmp')
        os.close(fd)
        logging.debug('Download destination path: %s' % self._dest_path)

        # We have to start the download to get 'total-size'
        # property. It not, 0 is returned
        self._download.set_destination_uri('file://' + self._dest_path)
        self._download.start()

    def __progress_change_cb(self, download, something):
        progress = self._download.get_progress()
        self.dl_jobject.metadata['progress'] = str(int(progress * 100))
        datastore.write(self.dl_jobject)

    def __state_change_cb(self, download, gparamspec):
        state = self._download.get_status()
        if state == WebKit.DownloadStatus.STARTED:
            # Check free space and cancel the download if there is not enough.
            total_size = self._download.get_total_size()
            logging.debug('Total size of the file: %s', total_size)
            enough_space = self.enough_space(
                total_size, path=self.temp_path)
            if not enough_space:
                logging.debug('Download canceled because of Disk Space')
                self.cancel()

                self._canceled_alert = Alert()
                self._canceled_alert.props.title = _('Not enough space '
                                                     'to download')

                total_size_mb = total_size / 1024.0 ** 2
                free_space_mb = self._free_available_space(
                    path=self.temp_path) - SPACE_THRESHOLD / 1024.0 ** 2
                filename = self._download.get_suggested_filename()
                self._canceled_alert.props.msg = \
                    _('Download "%{filename}" requires %{total_size_in_mb}'
                      ' MB of free space, only %{free_space_in_mb} MB'
                      ' is available' %
                      {'filename': filename,
                       'total_size_in_mb': format_float(total_size_mb),
                       'free_space_in_mb': format_float(free_space_mb)})
                ok_icon = Icon(icon_name='dialog-ok')
                self._canceled_alert.add_button(Gtk.ResponseType.OK,
                                                _('Ok'), ok_icon)
                ok_icon.show()
                self._canceled_alert.connect('response',
                                             self.__stop_response_cb)
                self._activity.add_alert(self._canceled_alert)
            else:
                self._download.connect('notify::progress',
                                       self.__progress_change_cb)
                self._create_journal_object()
                self._object_id = self.dl_jobject.object_id

                alert = TimeoutAlert(9)
                alert.props.title = _('Download started')
                alert.props.msg = _('%s' %
                                    self._download.get_suggested_filename())
                self._activity.add_alert(alert)
                alert.connect('response', self.__start_response_cb)
                alert.show()
                global _active_downloads
                _active_downloads.append(self)

        elif state == WebKit.DownloadStatus.FINISHED:
            self._stop_alert = Alert()
            self._stop_alert.props.title = _('Download completed')
            self._stop_alert.props.msg = \
                _('%s' % self._download.get_suggested_filename())
            open_icon = Icon(icon_name='zoom-activity')
            self._stop_alert.add_button(Gtk.ResponseType.APPLY,
                                        _('Show in Journal'), open_icon)
            open_icon.show()
            ok_icon = Icon(icon_name='dialog-ok')
            self._stop_alert.add_button(Gtk.ResponseType.OK, _('Ok'), ok_icon)
            ok_icon.show()
            self._activity.add_alert(self._stop_alert)
            self._stop_alert.connect('response', self.__stop_response_cb)
            self._stop_alert.show()

            if self._dest_path.endswith('.journal'):

                metadata, preview_data, file_path = \
                    utils.unpackage_ds_object(self._dest_path)
                original_object_id = metadata['original_object_id']
                for key in metadata.keys():
                    self.dl_jobject.metadata[key] = metadata[key]

                self.dl_jobject.metadata['preview'] = dbus.ByteArray(
                    preview_data)

                self.dl_jobject.file_path = file_path

                datastore.write(self.dl_jobject,
                                transfer_ownership=True,
                                reply_handler=self.__internal_save_cb,
                                error_handler=self.__internal_error_cb,
                                timeout=360)

                # notify to the server, the object was successfully downloaded
                url = 'ws://%s:%d/websocket' % (self._activity.ip,
                                                self._activity.port)
                messanger = utils.Messanger(url)
                data = utils.get_user_data()
                data['object_id'] = original_object_id
                messanger.send_message('DOWNLOADED', data)

            else:
                self.dl_jobject.metadata['title'] = \
                    self._download.get_suggested_filename()
                self.dl_jobject.metadata['description'] = _('From: %s') \
                    % self._source
                self.dl_jobject.metadata['progress'] = '100'
                self.dl_jobject.file_path = self._dest_path

                # sniff for a mime type, no way to get headers from WebKit
                sniffed_mime_type = mime.get_for_file(self._dest_path)
                self.dl_jobject.metadata['mime_type'] = sniffed_mime_type

                datastore.write(self.dl_jobject,
                                transfer_ownership=True,
                                reply_handler=self.__internal_save_cb,
                                error_handler=self.__internal_error_cb,
                                timeout=360)

        elif state == WebKit.DownloadStatus.CANCELLED:
            self.cleanup()

    def __error_cb(self, download, err_code, err_detail, reason):
        logging.debug('Error downloading URI code %s, detail %s: %s'
                      % (err_code, err_detail, reason))

    def __internal_save_cb(self):
        logging.debug('Object saved succesfully to the datastore.')
        self.cleanup()

    def __internal_error_cb(self, err):
        logging.debug('Error saving activity object to datastore: %s' % err)
        self.cleanup()

    def __start_response_cb(self, alert, response_id):
        global _active_downloads
        if response_id is Gtk.ResponseType.CANCEL:
            logging.debug('Download Canceled')
            self.cancel()
            try:
                datastore.delete(self._object_id)
            except Exception, e:
                logging.warning('Object has been deleted already %s' % e)

            self.cleanup()
            if self._stop_alert is not None:
                self._activity.remove_alert(self._stop_alert)

        self._activity.remove_alert(alert)

    def __stop_response_cb(self, alert, response_id):
        global _active_downloads
        if response_id is Gtk.ResponseType.APPLY:
            logging.debug('Start application with downloaded object')
            activity.show_object_in_journal(self._object_id)
        self._activity.remove_alert(alert)

    def cleanup(self):
        global _active_downloads
        if self in _active_downloads:
            _active_downloads.remove(self)

        if self.datastore_deleted_handler is not None:
            self.datastore_deleted_handler.remove()
            self.datastore_deleted_handler = None

        if os.path.isfile(self._dest_path):
            os.remove(self._dest_path)

        if self.dl_jobject is not None:
            self.dl_jobject.destroy()
            self.dl_jobject = None

    def cancel(self):
        self._download.cancel()

    def enough_space(self, size, path='/'):
        """Check if there is enough (size) free space on path

        size -- free space requested in Kb

        path -- device where the check will be done. For example: '/tmp'

        This method is useful to check the free space, for example,
        before starting a download from internet, creating a big map
        in some game or whatever action that needs some space in the
        Hard Disk.
        """

        free_space = self._free_available_space(path=path)
        return free_space - size > SPACE_THRESHOLD

    def _free_available_space(self, path='/'):
        s = os.statvfs(path)
        return s.f_bavail * s.f_frsize

    def _create_journal_object(self):
        self.dl_jobject = datastore.create()
        self.dl_jobject.metadata['title'] = \
            _('Downloading %(filename)s from \n%(source)s.') % \
            {'filename': self._download.get_suggested_filename(),
             'source': self._source}

        self.dl_jobject.metadata['progress'] = '0'
        self.dl_jobject.metadata['keep'] = '0'
        self.dl_jobject.metadata['buddies'] = ''
        self.dl_jobject.metadata['preview'] = ''
        self.dl_jobject.metadata['icon-color'] = \
            profile.get_color().to_string()
        self.dl_jobject.metadata['mime_type'] = ''
        self.dl_jobject.file_path = ''
        datastore.write(self.dl_jobject)

        bus = dbus.SessionBus()
        obj = bus.get_object(DS_DBUS_SERVICE, DS_DBUS_PATH)
        datastore_dbus = dbus.Interface(obj, DS_DBUS_INTERFACE)
        self.datastore_deleted_handler = datastore_dbus.connect_to_signal(
            'Deleted', self.__datastore_deleted_cb,
            arg0=self.dl_jobject.object_id)

    def __datastore_deleted_cb(self, uid):
        logging.debug('Downloaded entry has been deleted'
                      ' from the datastore: %r', uid)
        global _active_downloads
        if self in _active_downloads:
            self.cancel()
            self.cleanup()


def add_download(download, browser):
    download = Download(download, browser)
