# -*- coding: utf-8 -*-

#################################################################################################

import _strptime # Workaround for threads using datetime: _striptime is locked
import json
import logging
import sys
from datetime import datetime

import xbmc
import xbmcgui

import objects
import connect
import client
import library
import setup
import monitor
from libraries import requests
from views import Views, verify_kodi_defaults
from helper import _, window, settings, event, dialog, find, compare_version
from downloader import get_objects
from emby import Emby

#################################################################################################

LOG = logging.getLogger("EMBY."+__name__)

#################################################################################################


class Service(xbmc.Monitor):

    running = True
    library_thread = None
    monitor = None
    play_event = None
    warn = True
    settings = {'last_progress': datetime.today()}


    def __init__(self):

        window('emby_should_stop', clear=True)

        self.settings['addon_version'] = client.get_version()
        self.settings['profile'] = xbmc.translatePath('special://profile')
        self.settings['mode'] = settings('useDirectPaths')
        self.settings['log_level'] = settings('logLevel') or "1"
        self.settings['auth_check'] = True
        self.settings['enable_context'] = settings('enableContext.bool')
        self.settings['enable_context_transcode'] = settings('enableContextTranscode.bool')
        self.settings['kodi_companion'] = settings('kodiCompanion.bool')
        window('emby_logLevel', value=str(self.settings['log_level']))
        window('emby_kodiProfile', value=self.settings['profile'])

        if self.settings['enable_context']:
            window('emby_context', value="true")
        if self.settings['enable_context_transcode']:
            window('emby_context_transcode', value="true")

        LOG.warn("--->>>[ %s ]", client.get_addon_name())
        LOG.warn("Version: %s", client.get_version())
        LOG.warn("KODI Version: %s", xbmc.getInfoLabel('System.BuildVersion'))
        LOG.warn("Platform: %s", client.get_platform())
        LOG.warn("Python Version: %s", sys.version)
        LOG.warn("Using dynamic paths: %s", settings('useDirectPaths') == "0")
        LOG.warn("Log Level: %s", self.settings['log_level'])

        verify_kodi_defaults()
        Views().get_nodes()
        window('emby.connected.bool', True)

        xbmc.Monitor.__init__(self)
        self.check_update()
        settings('groupedSets.bool', objects.utils.get_grouped_set())

    def service(self):

        ''' Keeps the service monitor going.
            Exit on Kodi shutdown or profile switch.

            if profile switch happens more than once, 
            Threads depending on abortRequest will not trigger.
        '''
        self.monitor = monitor.Monitor()
        self.connect = connect.Connect()
        self.start_default()

        self.settings['mode'] = settings('useDirectPaths')

        while self.running:
            if window('emby_online.bool'):

                if self.settings['profile'] != window('emby_kodiProfile'):
                    LOG.info("[ profile switch ] %s", self.settings['profile'])

                    break

                if self.monitor.player.isPlaying():
                    difference = datetime.today() - self.settings['last_progress']

                    if difference.seconds > 10:

                        update = difference.seconds > 250
                        event('ReportProgressRequested', {'Report': update})
                        
                        if update:
                            self.settings['last_progress'] = datetime.today()

            if self.waitForAbort(1):
                break

        self.shutdown()

    def start_default(self):

        try:
            self.connect.register()
            setup.Setup()
        except Exception as error:
            LOG.error(error)

    def stop_default(self):

        window('emby_online', clear=True)
        Emby().close()

        if self.library_thread is not None:

            self.library_thread.stop_client()
            self.library_thread = None


    def check_update(self):

        ''' Check for objects build version and compare.
            This pulls a dict that contains all the information for the build needed.
        '''
        LOG.info("--[ check updates/%s ]", objects.version)
        kodi = xbmc.getInfoLabel('System.BuildVersion')

        try:
            versions = requests.get('http://kodi.emby.media/Public%20testing/Dependencies/databases.json').json()
            build = find(versions, kodi)

            if not build:
                raise Exception("build %s incompatible?!" % kodi)

            label, zipfile = build.split('-', 1)

            if label == objects.version:
                LOG.info("--[ objects/%s ]", objects.version)

                return

            if get_objects(zipfile, label + '.zip'):

                LOG.info("No previous patch found.")
                dialog("ok", heading="{emby}", line1=_(33135))
                xbmc.executebuiltin('RestartApp')
            else:
                dialog("notification", heading="{emby}", message=_(33156), icon="{emby}")

                try:
                    if compare_version(self.settings['addon_version'], objects.embyversion) < 0:
                        dialog("ok", heading="{emby}", line1="%s %s" % (_(33160), objects.embyversion))
                except Exception:
                    pass

        except Exception as error:
            LOG.exception(error)
    
    def onNotification(self, sender, method, data):

        ''' All notifications are sent via NotifyAll built-in or Kodi.
            Central hub.
        '''
        if sender.lower() not in ('plugin.video.emby', 'xbmc'):
            return

        if sender == 'plugin.video.emby':
            method = method.split('.')[1]

            if method not in ('ServerUnreachable', 'ServerShuttingDown', 'UserDataChanged', 'ServerConnect',
                              'LibraryChanged', 'ServerOnline', 'SyncLibrary', 'RepairLibrary', 'RemoveLibrary',
                              'EmbyConnect', 'SyncLibrarySelection', 'RepairLibrarySelection', 'AddServer',
                              'Unauthorized', 'UpdateServer', 'UserConfigurationUpdated', 'ServerRestarting',
                              'RemoveServer', 'AddLibrarySelection', 'CheckUpdate'):
                return

            data = json.loads(data)[0]
        else:
            if method not in ('System.OnQuit', 'System.OnSleep', 'System.OnWake'):
                return

            data = json.loads(data)

        LOG.debug("[ %s: %s ] %s", sender, method, json.dumps(data, indent=4))

        if method == 'ServerOnline':
            if data['ServerId'] is None:

                window('emby_online.bool', True)
                self.settings['auth_check'] = True
                self.warn = True

                if self.library_thread is None:

                    self.library_thread = library.Library(self)
                    self.library_thread.start()

        elif method in ('ServerUnreachable', 'ServerShuttingDown'):

            if self.warn or data.get('ServerId'):

                self.warn = data.get('ServerId') is not None
                dialog("notification", heading="{emby}", message=_(33146) if data.get('ServerId') is None else _(33149), icon=xbmcgui.NOTIFICATION_ERROR)

            if data.get('ServerId') is None:
                self.stop_default()

                if self.waitForAbort(20):
                    return
                
                self.start_default()

        elif method == 'Unauthorized':
            dialog("notification", heading="{emby}", message=_(33147) if data['ServerId'] is None else _(33148), icon=xbmcgui.NOTIFICATION_ERROR)

            if data.get('ServerId') is None and self.settings['auth_check']:

                self.settings['auth_check'] = False
                self.stop_default()

                if self.waitForAbort(5):
                    return
                
                self.start_default()

        elif method == 'ServerRestarting':
            if data.get('ServerId'):
                return
            
            if settings('restartMsg.bool'):
                dialog("notification", heading="{emby}", message=_(33006), icon="{emby}")

            self.stop_default()

            if self.waitForAbort(15):
                return
                
            self.start_default()

        elif method == 'ServerConnect':
            self.connect.register(data['Id'])
            xbmc.executebuiltin("Container.Refresh")

        elif method == 'EmbyConnect':
            self.connect.setup_login_connect()

        elif method == 'AddServer':

            self.connect.setup_manual_server()
            xbmc.executebuiltin("Container.Refresh")

        elif method == 'RemoveServer':

            self.connect.remove_server(data['Id'])
            xbmc.executebuiltin("Container.Refresh")

        elif method == 'UpdateServer':

            dialog("ok", heading="{emby}", line1=_(33151))
            self.connect.setup_manual_server()

        elif method == 'UserDataChanged' and self.library_thread:
            if data.get('ServerId'):
                return

            LOG.info("[ UserDataChanged ] %s", data)
            self.library_thread.userdata(data['UserDataList'])

        elif method == 'LibraryChanged' and self.library_thread:
            if data.get('ServerId'):
                return

            LOG.info("[ LibraryChanged ] %s", data)
            self.library_thread.updated(data['ItemsUpdated'] + data['ItemsAdded'])
            self.library_thread.removed(data['ItemsRemoved'])

        elif method == 'System.OnQuit':
            window('emby_should_stop.bool', True)
            self.running = False

        elif method in ('SyncLibrarySelection', 'RepairLibrarySelection', 'AddLibrarySelection'):
            self.library_thread.select_libraries(method)

        elif method == 'SyncLibrary':
            if not data.get('Id'):
                return

            self.library_thread.add_library(data['Id'])
            xbmc.executebuiltin("Container.Refresh")

        elif method == 'RepairLibrary':
            if not data.get('Id'):
                return

            libraries = data['Id'].split(',')

            for lib in libraries:
                self.library_thread.remove_library(lib)
            
            self.library_thread.add_library(data['Id'])
            xbmc.executebuiltin("Container.Refresh")

        elif method == 'RemoveLibrary':
            libraries = data['Id'].split(',')

            for lib in libraries:
                self.library_thread.remove_library(lib)

            xbmc.executebuiltin("Container.Refresh")

        elif method == 'System.OnSleep':
            
            LOG.info("-->[ sleep ]")
            window('emby_should_stop.bool', True)

            if self.library_thread is not None:

                self.library_thread.stop_client()
                self.library_thread = None

            Emby.close_all()
            self.monitor.server = []
            self.monitor.sleep = True

        elif method == 'System.OnWake':

            LOG.info("--<[ sleep ]")
            xbmc.sleep(10000)# Allow network to wake up
            self.monitor.sleep = False
            window('emby_should_stop', clear=True)

            try:
                self.connect.register()
            except Exception as error:
                LOG.error(error)

        elif method == 'GUI.OnScreensaverDeactivated':

            LOG.info("--<[ screensaver ]")
            xbmc.sleep(5000)

            if self.library_thread is not None:
                self.library_thread.fast_sync()

        elif method == 'UserConfigurationUpdated':

            if data.get('ServerId') is None:
                Views().get_views()

        elif method == 'CheckUpdate':
            if not self.check_update():
                dialog("notification", heading="{emby}", message=_(21341), icon="{emby}", sound=False)

    def onSettingsChanged(self):

        ''' React to setting changes that impact window values.
        '''
        if window('emby_should_stop.bool'):
            return

        if settings('logLevel') != self.settings['log_level']:

            log_level = settings('logLevel')
            window('emby_logLevel', str(log_level))
            self.settings['logLevel'] = log_level
            LOG.warn("New log level: %s", log_level)

        if settings('enableContext.bool') != self.settings['enable_context']:

            window('emby_context', settings('enableContext'))
            self.settings['enable_context'] = settings('enableContext.bool')
            LOG.warn("New context setting: %s", self.settings['enable_context'])

        if settings('enableContextTranscode.bool') != self.settings['enable_context_transcode']:

            window('emby_context_transcode', settings('enableContextTranscode'))
            self.settings['enable_context_transcode'] = settings('enableContextTranscode.bool')
            LOG.warn("New context transcode setting: %s", self.settings['enable_context_transcode'])

        if settings('useDirectPaths') != self.settings['mode'] and self.library_thread.started:

            self.settings['mode'] = settings('useDirectPaths')
            LOG.warn("New playback mode setting: %s", self.settings['mode'])

            if not self.settings.get('mode_warn'):

                self.settings['mode_warn'] = True
                dialog("yesno", heading="{emby}", line1=_(33118))

        if settings('kodiCompanion.bool') != self.settings['kodi_companion']:
            self.settings['kodi_companion'] = settings('kodiCompanion.bool')

            if not self.settings['kodi_companion']:
                dialog("ok", heading="{emby}", line1=_(33138))

    def shutdown(self):

        LOG.warn("---<[ EXITING ]")

        properties = [ # TODO: review
            "emby_state", "emby_serverStatus",
            "emby_syncRunning", "emby_dbCheck",
            "emby_currUser", "emby_dbScan",
            "emby_initialScan",

            "emby_play", "emby_online", "emby.connected", "emby.resume",
            "emby.external", "emby.external_check", "emby_deviceId", "emby_db_check", "emby_pathverified"
        ]
        for prop in properties:
            window(prop, clear=True)

        Emby.close_all()

        if self.library_thread is not None:
            self.library_thread.stop_client()

        if self.monitor is not None:
            self.monitor.listener.stop()

        LOG.warn("---<<<[ %s ]", client.get_addon_name())
