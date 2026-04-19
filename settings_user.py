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
@file      :settings_user.py
@author    :Jack Sun (jack.sun@quectel.com)
@brief     :User setting config.
@version   :2.2.0
@date      :2023-04-11 11:43:11
@copyright :Copyright (c) 2022
"""


class UserConfig:

    class _server:
        none = 0x0
        AliIot = 0x1
        ThingsBoard = 0x2

    class _loc_method:
        none = 0x0
        gps = 0x1
        cell = 0x2
        wifi = 0x4
        all = 0x7

    class _work_mode:
        cycle = 0x1
        intelligent = 0x2

    class _drive_behavior_code:
        none = 0x0
        sharply_start = 0x1
        sharply_stop = 0x2
        sharply_turn_left = 0x3
        sharply_turn_right = 0x4

    class _ota_upgrade_status:
        none = 0x0
        to_be_updated = 0x1
        updating = 0x2
        update_successed = 0x3
        update_failed = 0x4

    class _ota_upgrade_module:
        none = 0x0
        sys = 0x1
        app = 0x2

    class _button_type:
        level = "level"    # Normal switch behavior (Direct follow)
        toggle = "toggle"  # Momentary push-button (Toggle logic)

    class _sensor_type:
        dht22 = "DHT22"    # DHT22 Single-wire sensor
        aht10 = "AHT10"    # AHT10 I2C sensor
        uart  = "UART"     # UART Serial sensor

    class _pull_mode:
        pu = "PU"  # Internal Pull-Up (Normally High)
        pd = "PD"  # Internal Pull-Down (Normally Low)
        pdis = "PULL_DISABLE" # No internal pull-up or pull-down

    debug = 1

    log_level = "DEBUG"

    checknet_timeout = 60

    server = _server.ThingsBoard

    phone_num = ""

    low_power_alert_threshold = 20

    low_power_shutdown_threshold = 5

    over_speed_threshold = 50

    sw_ota = 1

    sw_ota_auto_upgrade = 1

    sw_voice_listen = 0

    sw_voice_record = 0

    sw_fault_alert = 1

    sw_low_power_alert = 1

    sw_over_speed_alert = 1

    sw_sim_abnormal_alert = 1

    sw_disassemble_alert = 1

    sw_drive_behavior_alert = 1

    drive_behavior_code = _drive_behavior_code.none

    loc_method = _loc_method.all

    loc_gps_read_timeout = 3000

    work_mode = _work_mode.cycle

    work_mode_timeline = 3600

    work_cycle_period = 10

    user_ota_action = -1

    ota_status = {
        "sys_current_version": "10",
        "sys_target_version": "20",
        "app_current_version": "30",
        "app_target_version": "50",
        "upgrade_module": _ota_upgrade_module.none,
        "upgrade_status": _ota_upgrade_status.none,
    }

    sw_mqtt_post = 1

    buttons = {
        "refill": {"pin": 7, "led_pin": 4, "type": _button_type.toggle, "pull": _pull_mode.pu, "key": "refill_request", "values": ["False", "True"]},
        "door": {"pin": 20, "led_pin": None, "type": _button_type.level, "pull": _pull_mode.pu, "key": "door_state", "values": ["Closed", "Open"]},
        "maintenance": {"pin": 19, "led_pin": 3, "type": _button_type.toggle, "pull": _pull_mode.pu, "key": "maintenance_request", "values": ["False", "True"]},
        "power": {"pin": 2, "led_pin": 1, "type": _button_type.level, "pull": _pull_mode.pdis, "key": "power_state", "values": ["Power ON", "Power OFF"], "invert_led": False},
        "poll_interval_ms": 50
    }

    http_config = {
        "url": "https://interactivemap-1-fhc0.onrender.com/api/location",
        "car_id": "EC200U-01",
        "sw_http_post": 1
    }

    sensor_config = {
        "type": _sensor_type.aht10,  # Default to aht10
        "pin": 0,                   # GPIO for DHT22
        "i2c_port": 0,               # Bus for AHT10 (I2C0 — proven in diag)
        "uart_port": 2,              # Port for UART sensor
    }
