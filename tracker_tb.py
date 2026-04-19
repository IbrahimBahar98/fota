# Copyright (c) Quectel Wireless Solution, Co., Ltd.All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
@file      :tracker_tb.py
@author    :Jack Sun (jack.sun@quectel.com)
@brief     :Tracker by ThingsBoard.
@version   :2.2.0
@date      :2023-04-14 14:30:13
@copyright :Copyright (c) 2022
"""

import utime
import _thread
import osTimer
import ujson
import request
from misc import Power
from queue import Queue
from machine import RTC

try:
    from settings import Settings, PROJECT_VERSION, FIRMWARE_VERSION
    from settings_user import UserConfig
    from modules.history import History
    from modules.logging import getLogger
    from modules.net_manage import NetManager
    from modules.thingsboard import TBDeviceMQTTClient
    from modules.power_manage import PowerManage, PMLock
    from modules.location import GNSS, GNSSBase, CellLocator, WiFiLocator, CoordinateSystemConvert
    from modules.buttons import DeviceButtons
    from modules.led_control import LEDManager
    from modules.upgrade_manager import UpgradeManager
except ImportError:
    from usr.settings import Settings, PROJECT_VERSION, FIRMWARE_VERSION
    from usr.settings_user import UserConfig
    from usr.modules.history import History
    from usr.modules.logging import getLogger
    from usr.modules.net_manage import NetManager
    from usr.modules.thingsboard import TBDeviceMQTTClient
    from usr.modules.power_manage import PowerManage, PMLock
    from usr.modules.location import GNSS, GNSSBase, CellLocator, WiFiLocator, CoordinateSystemConvert
    from usr.modules.buttons import DeviceButtons
    from usr.modules.led_control import LEDManager
    from usr.modules.upgrade_manager import UpgradeManager

# Optional: Alarm Manager
try:
    from modules.alarm_manager import AlarmManager, ALARM_DEFINITIONS
except ImportError:
    try:
        from usr.modules.alarm_manager import AlarmManager, ALARM_DEFINITIONS
    except ImportError:
        AlarmManager = None
        ALARM_DEFINITIONS = None

log = getLogger(__name__)


class Tracker:

    def __init__(self):
        self.__server = None
        self.__server_ota = None
        self.__history = None
        self.__gnss = None
        self.__cell = None
        self.__wifi = None
        self.__csc = None
        self.__net_manager = None
        self.__settings = None
        self.__buttons = None
        self.__sensor = None
        self.__upgrade = None
        self.__pm = None
        self.__leds = LEDManager()
        self.__alarm_manager = None
        
        # State tracking for sensor change threshold
        self.__last_temp = None
        self.__last_humi = None

        self.__business_lock = PMLock("block")
        self.__business_tid = None
        self.__business_rtc = RTC()
        self.__business_queue = Queue()
        self.__server_ota_flag = 0
        self.__ota_report_done = False
        self.__running_tag = 0
        self.__server_reconn_timer = osTimer()
        self.__server_conn_tag = 0
        self.__server_reconn_count = 0
        self.__reset_tag = 0
        self.__fota_in_progress = False

    def __business_start(self):
        if not self.__business_tid or (self.__business_tid and not _thread.threadIsRunning(self.__business_tid)):
            _thread.stack_size(0x2000)
            self.__business_tid = _thread.start_new_thread(self.__business_running, ())

    def __business_stop(self):
        self.__business_tid = None
        # Send sentinel to unblock the queue.get() and trigger termination
        self.__business_queue.put((-1, "exit"))
        
        # Give thread a moment to see the sentinel and exit
        utime.sleep_ms(200)
        
        # Drain remaining items to ensure a clean slate on restart
        while self.__business_queue.size() > 0:
            try: self.__business_queue.get(block=False)
            except: break

    def __business_running(self):
        while True:
            data = self.__business_queue.get()
            if data[0] == -1:
                break
                
            with self.__business_lock:
                if data[0] == 0:
                    if data[1] == "loc_report":
                        self.__loc_report()
                    elif data[1] == "server_connect":
                        self.__server_connect()
                    elif data[1] == "telemetry_update":
                        self.__telemetry_report(data[2])
                    elif data[1] == "ota_refresh":
                        self.__ota_refresh()
                    elif data[1] == "into_sleep":
                        self.__into_sleep()
                if data[0] == 1:
                    self.__server_option(data[1])

    def __loc_report(self):
        # Report current location.
        loc_state, properties = self.__get_loc_data()
        if loc_state == 1:
            # User custom format: {"lat": Latitude, "long": Longitude}
            payload = {
                "lat": properties.get("Latitude"),
                "long": properties.get("Longitude")
            }
                
            res = False
            user_cfg = self.__settings.read("user")
            if self.__server.status and user_cfg.get("sw_mqtt_post") == 1:
                # Custom topic: /device/<client_id>/location
                topic = "/device/" + self.__server.client_id + "/location"
                res = self.__server.publish(topic, payload)
            
            # --- SECONDARY HTTP REPORTING ---
            user_cfg = self.__settings.read("user")
            http_cfg = user_cfg.get("http_config", {})
            if http_cfg.get("sw_http_post") == 1:
                self.__http_post_report(
                    properties.get("Latitude"),
                    properties.get("Longitude"),
                    properties.get("Altitude"),
                    properties.get("Speed")
                )
            
            if not res:
                self.__history.write([payload])

        # Report history location.
        if self.__server.status:
            self.__history_report()

        # Report telemetry (UART DHT) to the cloud synchronously with location cycle.
        self.__telemetry_report()

    def __history_report(self):
        failed_datas = []
        his_datas = self.__history.read()
        if his_datas["data"]:
            for item in his_datas["data"]:
                # Ensure backwards compatibility for old saved history data
                payload = item if "data" in item else {"data": item}
                res = self.__server.send_telemetry(payload)
                if not res:
                    failed_datas.append(item)
        if failed_datas:
            self.__history.write(failed_datas)

    def __http_post_report(self, lat, lon, alt=None, speed=None):
        """Perform secondary location POST to the Render server."""
        user_cfg = self.__settings.read("user")
        cfg = user_cfg.get("http_config", {})
        url = cfg.get("url")
        car_id = cfg.get("car_id", "EC200U-Unknown")

        if not url:
            log.warning("[HTTP] No URL configured for secondary reporting.")
            return

        payload = {
            "latitude": round(lat, 6),
            "longitude": round(lon, 6),
            "carID": car_id,
        }
        if alt is not None:
            payload["altitude"] = round(alt, 1)
        if speed is not None:
            payload["speed_knots"] = round(speed, 1)

        headers = {'Content-Type': 'application/json'}
        try:
            log.debug("[HTTP] Posting to %s" % url)
            response = request.post(url, data=ujson.dumps(payload), headers=headers)
            log.info("[HTTP] Status: %d | Response: %s" % (response.status_code, response.text))
            response.close()
        except Exception as e:
            log.error("[HTTP] POST failed: %s" % str(e))

    def __telemetry_report(self, states=None):
        if not self.__server or not self.__server.status:
            return
            
        if states is None:
            states = {}
            
        payload = {"data": {}}
        
        # Dynamically append all button states to telemetry payload
        for key, val in states.items():
            payload["data"][key] = {"value": val}
        # Read Sensor if available, only append if change >= 0.1 or first read
        if self.__sensor:
            # Note: status == 0 is OK
            status, t, h = self.__sensor.read()
            if status == 0:
                # Format to 1 decimal place
                t_formatted = float("{:.1f}".format(t))
                h_formatted = float("{:.1f}".format(h))
                
                # Always report temp and humidity
                payload["data"]["temperature"] = {"value": t_formatted}
                self.__last_temp = t_formatted
                log.debug("[Sensor] Temperature: %.1f" % t_formatted)
                payload["data"]["humidity"] = {"value": h_formatted}
                self.__last_humi = h_formatted
                log.debug("[Sensor] Humidity: %.1f" % h_formatted)
            else:
                log.error("[Sensor] Read failed with status: %d" % status)
                
        # Send telemetry if payload data is not empty
        if payload["data"]:
            log.info("[Telemetry] Sending payload: %s" % str(payload))
            res = self.__server.send_telemetry(payload)
            if not res:
                log.error("[Telemetry] Failed to send to cloud.")
        else:
            log.debug("[Telemetry] Nothing to report (no changes).")

    def __get_loc_data(self):
        loc_state = 0
        loc_data = {
            "Longitude": 0.0,
            "Latitude": 0.0,
            "Altitude": 0.0,
            "Speed": 0.0,
        }
        loc_cfg = self.__settings.read("loc")
        user_cfg = self.__settings.read("user")
        if self.__gnss and user_cfg["loc_method"] & UserConfig._loc_method.gps:
            res = self.__gnss.read()
            log.debug("gnss read %s" % str(res))
            if res["state"] == "A":
                loc_data["Latitude"] = float(res["lat"]) * (1 if res["lat_dir"] == "N" else -1)
                loc_data["Longitude"] = float(res["lng"]) * (1 if res["lng_dir"] == "E" else -1)
                loc_data["Altitude"] = float(res["altitude"]) if res["altitude"] else 0.0
                loc_data["Speed"] = float(res["speed"]) if res["speed"] else 0.0
                loc_state = 1
        if loc_state == 0 and user_cfg["loc_method"] & UserConfig._loc_method.cell:
            res = self.__cell.read()
            if isinstance(res, tuple):
                loc_data["Longitude"] = res[0]
                loc_data["Latitude"] = res[1]
                loc_state = 1
        if loc_state == 0 and user_cfg["loc_method"] & UserConfig._loc_method.wifi:
            res = self.__wifi.read()
            if isinstance(res, tuple):
                loc_data["Longitude"] = res[0]
                loc_data["Latitude"] = res[1]
                loc_state = 1
        if loc_state == 1 and loc_cfg["map_coordinate_system"] == "GCJ02":
            lng, lat = self.__csc.wgs84_to_gcj02(loc_data["Longitude"], loc_data["Latitude"])
            loc_data["Longitude"] = lng
            loc_data["Latitude"] = lat
        return (loc_state, loc_data)

    def __ota_refresh(self):
        """Report current firmware and app versions to server."""
        if not self.__server or not self.__server.status:
            return
            
        payload = {
            "fw_version": FIRMWARE_VERSION,
            "app_version": PROJECT_VERSION
        }
        log.info("[OTA] Refreshing versions: %s" % str(payload))
        # Report as telemetry (more reliable for version reporting on some platforms)
        self.__server.send_telemetry(payload)

    def __into_sleep(self):
        """Manager device sleep transition."""
        user_cfg = self.__settings.read("user")
        log.info("[Business] Sequence complete. Entering sleep for %d seconds." % user_cfg["work_cycle_period"])
        
        # Adjust sleep mode
        if self.__pm:
            if user_cfg["work_cycle_period"] < user_cfg.get("work_mode_timeline", 3600):
                self.__pm.autosleep(1)
            else:
                self.__pm.set_psm(mode=1, tau=user_cfg["work_cycle_period"], act=5)
                
        # Set RTC to wake up and run the sequence again
        self.__set_rtc(user_cfg["work_cycle_period"], self.running)

    def __set_rtc(self, period, callback):
        self.__business_rtc.enable_alarm(0)
        if callback and callable(callback):
            self.__business_rtc.register_callback(callback)
        atime = utime.localtime(utime.mktime(utime.localtime()) + period)
        alarm_time = (atime[0], atime[1], atime[2], atime[6], atime[3], atime[4], atime[5], 0)
        _res = self.__business_rtc.set_alarm(alarm_time)
        log.debug("alarm_time: %s, set_alarm res %s." % (str(alarm_time), _res))
        return self.__business_rtc.enable_alarm(1) if _res == 0 else -1

    def __server_connect(self):
        if self.__net_manager.net_status():
            self.__server.disconnect()
            self.__server.connect()
        if not self.__server.status:
            self.__server_reconn_timer.stop()
            self.__server_reconn_timer.start(60 * 1000, 0, self.server_connect)
            self.__server_reconn_count += 1
        else:
            self.__server_reconn_count = 0

        # When server not connect success after 20 miuntes, to reset device.
        if self.__server_reconn_count >= 20:
            _thread.stack_size(0x1000)
            _thread.start_new_thread(self.__power_restart, ())
        self.__server_conn_tag = 0

    def __server_option(self, args):
        topic, data = args
        log.debug("topic[%s]data[%s]" % args)
        
        try:
            # Handle plain string commands before JSON parsing
            if isinstance(data, (str, bytes)) and str(data).strip().lower() == "reset":
                log.info("[RPC] Remote RESET command received. Rebooting in 2s...")
                utime.sleep(2)
                Power.powerRestart()
                return
            
            if isinstance(data, (str, bytes)):
                data = ujson.loads(data)
            
            method = data.get("method")
            params = data.get("params", {})
            log.debug("method[%s] params[%s]" % (method, params))
            
            # --- RPC Handling ---
            # Check for fields at top-level OR inside params
            url = data.get("url") or params.get("url")
            file_name = data.get("file_name") or params.get("file_name")
            files = data.get("files") or params.get("files")
            
            if method == "reset":
                log.info("[RPC] Remote RESET command received. Rebooting in 2s...")
                utime.sleep(2)
                Power.powerRestart()
            elif method == "ota_firmware_upgrade" and self.__upgrade:
                self.__upgrade.firmware_upgrade(url)
            elif method == "ota_app_upgrade" and self.__upgrade:
                if self.__fota_in_progress:
                    log.warning("[OTA] Rejecting RPC: FOTA already in progress.")
                    return
                    
                file_list = []
                if url and file_name:
                    file_list = [{"url": url, "file_name": file_name}]
                else:
                    file_list = files
                    
                import _thread
                _thread.stack_size(0x2000)
                _thread.start_new_thread(self.__execute_fota_sequence, (file_list,))
            else:
                log.info("Unknown RPC method: %s" % str(method))
        except Exception as e:
            log.error("Error parsing server option: %s" % str(e))

    def __execute_fota_sequence(self, file_list):
        import utime
        self.__fota_in_progress = True
        log.info("[Tracker] Initiating graceful shutdown for FOTA...")
        
        try:
            # 1. Stop business tasks
            self.__business_stop()
            
            # 2. Stop reconnections
            try:
                self.__server_reconn_timer.stop()
            except: pass
            
            # 3. Cleanly kill the MQTT Thread loop
            # This prevents the ECONNABORTED lock
            if self.__server:
                log.info("[Tracker] Disconnecting MQTT Client...")
                try: self.__server.disconnect()
                except: pass
                
            # Give business thread time to finish current task and exit
            for _ in range(30):
                if not self.__business_tid or not _thread.threadIsRunning(self.__business_tid):
                    break
                utime.sleep(0.1)
            
            log.info("[Tracker] Business thread has exited cleanly.")
            
            # 4. Wait for network stack and sockets to clear
            log.info("[Tracker] Waiting for network stack to settle...")
            utime.sleep(5)
            
            # 5. Hand over to upgrade manager!
            res = self.__upgrade.app_upgrade(file_list)
            
            # 6. RECOVERY: If FOTA failed (res is False), it won't reboot. 
            # We must revive the disconnected services to prevent stranding!
            if not res:
                log.error("[Tracker] FOTA failed! Recovering background services...")
                self.__business_start()
                self.server_connect(None)
                # Re-arm the business cycle since the previous RTC alarm was consumed
                self.__business_queue.put((0, "loc_report"))
                self.__business_queue.put((0, "into_sleep"))
        finally:
            self.__fota_in_progress = False
            
    def __power_restart(self):
        if self.__reset_tag == 1:
            return
        self.__reset_tag = 1
        count = 0
        while self.__business_queue.size() > 0 and count < 30:
            count += 1
            utime.sleep(1)
        log.debug("__power_restart")
        Power.powerRestart()

    def add_module(self, module):
        if isinstance(module, TBDeviceMQTTClient):
            self.__server = module
        elif isinstance(module, History):
            self.__history = module
        elif isinstance(module, GNSSBase):
            self.__gnss = module
        elif isinstance(module, CellLocator):
            self.__cell = module
        elif isinstance(module, WiFiLocator):
            self.__wifi = module
        elif isinstance(module, CoordinateSystemConvert):
            self.__csc = module
        elif isinstance(module, NetManager):
            self.__net_manager = module
        elif isinstance(module, Settings):
            self.__settings = module
        elif isinstance(module, DeviceButtons):
            self.__buttons = module
        elif getattr(module, "_is_sensor", False):
            # Supports any generic sensor marked with _is_sensor
            self.__sensor = module
        elif isinstance(module, UpgradeManager):
            self.__upgrade = module
        elif isinstance(module, PowerManage):
            self.__pm = module
        elif isinstance(module, AlarmManager):
            self.__alarm_manager = module
        else:
            return False
        return True

    def running(self, args=None):
        if self.__fota_in_progress:
            log.warning("[Business] RTC wakeup ignored: FOTA in progress.")
            return
            
        if self.__running_tag == 1:
            return
        self.__running_tag = 1
        
        log.info("[Business] Starting task sequence...")
        
        # Disable sleep while working
        if self.__pm:
            self.__pm.autosleep(0)
            
        self.__business_start()
        
        # 1. Connect to server if not connected
        if not self.__server.status:
            self.__business_queue.put((0, "server_connect"))
            
        # 2. OTA Refresh (Only Once per startup)
        if not self.__ota_report_done:
            self.__business_queue.put((0, "ota_refresh"))
            self.__ota_report_done = True
        
        # 3. Location Report
        self.__business_queue.put((0, "loc_report"))
        
        # 4. Into Sleep
        self.__business_queue.put((0, "into_sleep"))
        
        self.__running_tag = 0

    def server_callback(self, topic, data):
        self.__business_queue.put((1, (topic, data)))

    def net_callback(self, args):
        log.debug("net_callback args: %s" % str(args))
        if args[1] == 0:
            self.__server.disconnect()
            self.__server_reconn_timer.stop()
            self.__server_reconn_timer.start(30 * 1000, 0, self.server_connect)
        else:
            self.__server_reconn_timer.stop()
            self.server_connect(None)

    def loc_report(self, args):
        self.__business_queue.put((0, "loc_report"))

    def server_connect(self, args):
        if self.__server_conn_tag == 0:
            self.__server_conn_tag = 1
            self.__business_queue.put((0, "server_connect"))
            
    def on_buttons_change(self, states):
        if not hasattr(self, "_last_sent_telemetry"):
            self._last_sent_telemetry = {}
            
        changed_states = {}
        for k, v in states.items():
            if self._last_sent_telemetry.get(k) != v:
                changed_states[k] = v
                self._last_sent_telemetry[k] = v
                
        if not changed_states:
            return
            
        self.__leds.update_states(changed_states)
        self.__business_queue.put((0, "telemetry_update", changed_states))
        
        if not hasattr(self, "_last_saved_states"):
            self._last_saved_states = {
                "refill_request": states.get("refill_request"),
                "maintenance_request": states.get("maintenance_request"),
                "conversion_rate": states.get("conversion_rate", 0)
            }
            # Avoid saving on very first boot initialization unless it changed
            must_save = False
        else:
            must_save = False
            
        # --- Alarm Handling ---
        # 1. Power State Alarm (Fridge Power Lost)
        power_val = states.get("power_state")
        power_is_on = True if power_val and (str(power_val).lower() in ["true", "1"] or "on" in str(power_val).lower()) else False
        
        last_power = getattr(self, "_last_power_state", True)
        if not power_is_on and last_power:
            # Transition ON -> OFF
            if self.__alarm_manager:
                self.__alarm_manager.set_alarm("fridge_power")
        elif power_is_on and not last_power:
            # Transition OFF -> ON
            if self.__alarm_manager:
                self.__alarm_manager.clear_alarm("fridge_power")
        self._last_power_state = power_is_on

        # 2. Maintenance & Refill Alarms
        for k, alarm_key in [("maintenance_request", "maintenance"), ("refill_request", "refill")]:
            current_val = states.get(k)
            # Use states.get(k) comparison for triggers
            is_active = True if current_val and (str(current_val).lower() in ["true", "1"]) else False
            
            # Use _last_sent_telemetry to detect transitions since last check
            # Note: states passed to this function are already the "current" ones.
            # We compare with the snapshot before the update in this loop.
            if self.__alarm_manager:
                if is_active:
                    self.__alarm_manager.set_alarm(alarm_key)
                else:
                    self.__alarm_manager.clear_alarm(alarm_key)

        # Save state when power is disconnected
        
        # Save state also when any persistent toggle values change
        for k in ["refill_request", "maintenance_request", "conversion_rate"]:
            if states.get(k) != self._last_saved_states.get(k):
                must_save = True
                
        if must_save and self.__settings:
            user_cfg = self.__settings.read("user")
            if "saved_states" not in user_cfg:
                user_cfg["saved_states"] = {}
            
            # Update saved states
            user_cfg["saved_states"]["refill_request"] = states.get("refill_request")
            user_cfg["saved_states"]["maintenance_request"] = states.get("maintenance_request")
            if "conversion_rate" in states:
                user_cfg["saved_states"]["conversion_rate"] = states.get("conversion_rate")
            
            self.__settings.save({"user": user_cfg})
            log.info("[Tracker] Persisted critical states to Flash.")
            
            # Sync cache
            for k in ["refill_request", "maintenance_request", "conversion_rate"]:
                self._last_saved_states[k] = states.get(k)


if __name__ == "__main__":
    # Init settings.
    settings = Settings()
    # Init history
    history = History()
    # Init power manage and set device low energy.
    power_manage = PowerManage()
    power_manage.autosleep(1)
    # Init net modules and start net connect.
    net_manager = NetManager()
    _thread.stack_size(0x1000)
    _thread.start_new_thread(net_manager.net_connect, ())
    # Init GNSS modules and start reading and parsing gnss data.
    loc_cfg = settings.read("loc")
    gnss = GNSS(**loc_cfg["gps_cfg"])
    gnss.set_trans(0)
    gnss.start()
    # Init cell and wifi location modules.
    cell = CellLocator(**loc_cfg["cell_cfg"])
    wifi = WiFiLocator(**loc_cfg["wifi_cfg"])
    # Init coordinate system convert modules.
    cyc = CoordinateSystemConvert()
    # Init server modules.
    server_cfg = settings.read("server")
    server = TBDeviceMQTTClient(**server_cfg)
    # Init upgrade manager for FOTA (firmware + app OTA).
    upgrade = UpgradeManager()
    # Init tracker business modules.
    tracker = Tracker()
    tracker.add_module(settings)
    tracker.add_module(history)
    tracker.add_module(net_manager)
    tracker.add_module(server)
    tracker.add_module(gnss)
    tracker.add_module(cell)
    tracker.add_module(wifi)
    tracker.add_module(cyc)
    tracker.add_module(upgrade)
    tracker.add_module(power_manage)
    
    # Init Alarm Manager with the registry
    alarm_manager = AlarmManager(server, ALARM_DEFINITIONS)
    tracker.add_module(alarm_manager)

    # Init Buttons and connect to Tracker callback
    user_cfg = settings.read("user")
    buttons = DeviceButtons(tracker.on_buttons_change, user_cfg.get("buttons"), user_cfg.get("saved_states"))
    tracker.add_module(buttons)
    
    # Set net modules callback.
    net_manager.set_callback(tracker.net_callback)
    # Set server modules callback.
    server.set_callback(tracker.server_callback)
    # Start tracker business.
    tracker.running()
