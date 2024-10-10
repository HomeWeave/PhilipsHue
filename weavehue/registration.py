from threading import Thread

from pyhuelights.discovery import DefaultDiscovery, DiscoveryFailed
from pyhuelights.registration import RegistrationWatcher
from pyhuelights.registration import REGISTRATION_FAILED, REGISTRATION_SUCCEEDED
from pyhuelights.registration import AuthenticatedHueConnection

from pyantonlib.utils import log_info, log_warn


class HueRegistrationController:

    def __init__(self, plugin, settings, callback):
        self.settings = settings
        self.plugin = plugin
        self.callback = callback
        self.conn = None
        self.watcher = None

    def start_discovery(self):
        Thread(target=self.discover_bridges).start()

    def discover_bridges(self):
        self.callback({"status": "discovering"})

        discovery = DefaultDiscovery()
        conn = None
        for x in range(3):
            try:
                log_info("Discovering Hue Bridges..")
                conn = discovery.discover()
                break
            except DiscoveryFailed:
                log_warn("No Hue bridges found.")
                continue

        if not conn:
            self.callback({"status": "not found"})
            return

        username = self.settings.get_prop("username")
        if username:
            self.conn = AuthenticatedHueConnection(conn.host, username)
            self.callback({
                "status": "connected",
                "host": self.conn.host,
                "conn": self.conn
            })
            log_info("Connected to Hue at: " + conn.host)
        else:
            self.conn = conn
            self.callback({"status": "unregistered", "host": conn.host})
            log_info("Found Hue at: " + conn.host)

    def register_bridge(self):
        if not self.conn or isinstance(self.conn, AuthenticatedHueConnection):
            self.callback({"status": "not found"})
            return

        self.watcher = RegistrationWatcher(
            self.conn.host, "PyHueLights#Anton", 30.0,
            lambda: self.registration_callback())
        self.watcher.start()

        self.callback({"status": "waiting", "host": self.conn.host})
        return True

    def registration_callback(self):
        if self.watcher.status == REGISTRATION_FAILED:
            self.callback({"status": "registration error"})
        elif self.watcher.status == REGISTRATION_SUCCEEDED:
            self.settings.set_prop("username", self.watcher.username)
            self.conn = AuthenticatedHueConnection(self.conn.host,
                                                   self.watcher.username)
            self.callback({
                "status": "connected",
                "host": self.conn.host,
                "conn": self.conn
            })

        self.watcher = None
