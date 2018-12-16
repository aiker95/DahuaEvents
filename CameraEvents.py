"""
Attach event listener to Dahua devices
Borrowed code from https://github.com/johnnyletrois/dahua-watch
And https://github.com/SaWey/home-assistant-dahua-event
Author: PsycikNZ
"""


REQUIREMENTS = ['pycurl>=7']

import threading
import requests
import re
import ConfigParser
import logging
import os
import socket
import pycurl
import time
import paho.mqtt.client as paho   # pip install paho-mqtt

mqttc = paho.Client("CameraEvents", clean_session=True)

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)

# create formatter
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# add formatter to ch
ch.setFormatter(formatter)
_LOGGER.addHandler(ch)



def setup( config):
    """Set up Dahua event listener."""
    #config = config.get(DOMAIN)

    dahua_event = DahuaEventThread(
        config
    )

    def _start_dahua_event(_event):
        dahua_event.start()

    def _stop_dahua_event(_event):
        dahua_event.stopped.set()

    return True


class DahuaDevice():
    #EVENT_TEMPLATE = "{protocol}://{host}:{port}/cgi-bin/eventManager.cgi?action=attach&channel=0&codes=%5B{events}%5D"
    EVENT_TEMPLATE = "{protocol}://{host}:{port}/cgi-bin/eventManager.cgi?action=attach&codes=%5B{events}%5D"
    CHANNEL_TEMPLATE = "{protocol}://{host}:{port}/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"

    def __init__(self, mqtt, name, device_cfg):
        self.channels = {}
        self.Name = name
        self.CurlObj = None
        self.Connected = None
        self.Reconnect = None
        self.user = device_cfg.get("user")
        self.password = device_cfg.get("pass")
        self.auth = device_cfg.get("auth")
        self.mqtt = mqtt

        #generate the event url
        self.url = self.EVENT_TEMPLATE.format(
            protocol=device_cfg.get("protocol"),
            host=device_cfg.get("host"),
            port=device_cfg.get("port"),
            events=device_cfg.get("events")
            
        )
        self.isNVR = False
        try:
            # Get NVR parm, to get channel names if NVR
            self.isNVR = device_cfg.get("isNVR")

            if self.isNVR:
                #generate the channel url
                self.channelurl  = self.CHANNEL_TEMPLATE.format(
                    protocol=device_cfg.get("protocol"),
                    host=device_cfg.get("host"),
                    port=device_cfg.get("port")
                )
                
                # get channel names here
                #table.ChannelTitle[0].Name=Garage
                response = requests.get(self.channelurl,auth=requests.auth.HTTPDigestAuth(self.user,self.password))
                for line in response.text.splitlines():
                    match = re.search('.\[(?P<index>[0-4])\]\..+\=(?P<channel>.+)',line)
                    if match:
                        _index = int(match.group("index"))
                        _channel = match.group("channel")
                        self.channels[_index] = _channel
            else:
                self.channels[0] = self.Name

        except Exception,e:
            _LOGGER.debug("Device " + name + " is not an NVR: " + str(e))
            _LOGGER.debug("Device " + name + " is not an NVR")
        
    


    def OnConnect(self):
        _LOGGER.debug("[{0}] OnConnect()".format(self.Name))
        self.Connected = True

    def OnDisconnect(self, reason):
        _LOGGER.debug("[{0}] OnDisconnect({1})".format(self.Name, reason))
        self.Connected = False


    def OnReceive(self, data):
        Data = data.decode("utf-8", errors="ignore")
        _LOGGER.debug("[{0}]: {1}".format(self.Name, Data))

        for Line in Data.split("\r\n"):
            if Line == "HTTP/1.1 200 OK":
                self.OnConnect()

            if not Line.startswith("Code="):
                continue

            Alarm = dict()
            Alarm["name"] = self.Name
            for KeyValue in Line.split(';'):
                Key, Value = KeyValue.split('=')
                Alarm[Key] = Value

            index =  int( Alarm["index"]         )
            if index in self.channels:
                Alarm["channel"] = self.channels[index]
            else:
                Alarm["channel"] = self.Name + ":" + index

            mqttc.connect(self.mqtt["IP"], int(self.mqtt["port"]), 60)
            if Alarm["Code"] == "VideoMotion":
                _LOGGER.info("Video Motion received: "+  Alarm["name"] + " Index: " + Alarm["channel"] + " Code: " + Alarm["Code"])
                if Alarm["action"] == "Start":
                    mqttc.publish("CameraEventsPy/" + Alarm["Code"] + "/" + Alarm["channel"] ,"ON")
                else:
                    mqttc.publish("CameraEventsPy/" + Alarm["Code"] + "/" + Alarm["channel"] ,"OFF")
            else:
                _LOGGER.info("dahua_event_received: "+  Alarm["name"] + " Index: " + Alarm["channel"] + " Code: " + Alarm["Code"])
                mqttc.publish("CameraEventsPy/" + Alarm["index"] + "/" + Alarm["name"],Alarm["Code"])

            mqttc.disconnect()
            #self.hass.bus.fire("dahua_event_received", Alarm)

class DahuaEventThread(threading.Thread):
    """Connects to device and subscribes to events"""
    Devices = []
    NumActivePlayers = 0

    CurlMultiObj = pycurl.CurlMulti()
    NumCurlObjs = 0
	

    def __init__(self,  mqtt, cameras):
        """Construct a thread listening for events."""

        for device_cfg in cameras:

            device = DahuaDevice(mqtt, device_cfg.get("name"), device_cfg)
            self.Devices.append(device)

            CurlObj = pycurl.Curl()
            device.CurlObj = CurlObj

            CurlObj.setopt(pycurl.URL, device.url)
            CurlObj.setopt(pycurl.CONNECTTIMEOUT, 30)
            CurlObj.setopt(pycurl.TCP_KEEPALIVE, 1)
            CurlObj.setopt(pycurl.TCP_KEEPIDLE, 30)
            CurlObj.setopt(pycurl.TCP_KEEPINTVL, 15)
            if device.auth == 'digest':
                CurlObj.setopt(pycurl.HTTPAUTH, pycurl.HTTPAUTH_DIGEST)
                CurlObj.setopt(pycurl.USERPWD, "%s:%s" % (device.user, device.password))
            else:
                CurlObj.setopt(pycurl.HTTPAUTH, pycurl.HTTPAUTH)
                CurlObj.setopt(pycurl.USERPWD, "%s:%s" % (device.user, device.password))
            CurlObj.setopt(pycurl.WRITEFUNCTION, device.OnReceive)

            self.CurlMultiObj.add_handle(CurlObj)
            self.NumCurlObjs += 1

            _LOGGER.debug("Added Dahua device at: %s", device.url)

        threading.Thread.__init__(self)
        self.stopped = threading.Event() 


    def run(self):
        """Fetch events"""
        while 1:
            Ret, NumHandles = self.CurlMultiObj.perform()
            if Ret != pycurl.E_CALL_MULTI_PERFORM:
                break

        Ret = self.CurlMultiObj.select(1.0)
        while not self.stopped.isSet():
            # Sleeps to ease load on processor
            time.sleep(.05)
            Ret, NumHandles = self.CurlMultiObj.perform()

            if NumHandles != self.NumCurlObjs:
                _, Success, Error = self.CurlMultiObj.info_read()

                for CurlObj in Success:
                    DahuaDevice = next(iter(filter(lambda x: x.CurlObj == CurlObj, self.Devices)), None)
                    if DahuaDevice.Reconnect:
                        _LOGGER.debug("Dahua Reconnect: %s", DahuaDevice.Name)
                        continue

                    DahuaDevice.OnDisconnect("Success")
                    DahuaDevice.Reconnect = time.time() + 5

                for CurlObj, ErrorNo, ErrorStr in Error:
                    DahuaDevice = next(iter(filter(lambda x: x.CurlObj == CurlObj, self.Devices)), None)
                    if DahuaDevice.Reconnect:
                        continue

                    DahuaDevice.OnDisconnect("{0} ({1})".format(ErrorStr, ErrorNo))
                    DahuaDevice.Reconnect = time.time() + 5

                for DahuaDevice in self.Devices:
                    if DahuaDevice.Reconnect and DahuaDevice.Reconnect < time.time():
                        self.CurlMultiObj.remove_handle(DahuaDevice.CurlObj)
                        self.CurlMultiObj.add_handle(DahuaDevice.CurlObj)
                        DahuaDevice.Reconnect = None
            #if Ret != pycurl.E_CALL_MULTI_PERFORM: break

if __name__ == '__main__':

    cameras = []
    cp = ConfigParser.ConfigParser()
    cp.read("config.ini")
    camera_items = cp.items( "Cameras" )
    for key, camera_key in camera_items:
        #do something with path
        camera_cp = cp.items(camera_key)
        camera = {}
        #temp = cp.get(camera_key,"host")
        camera["host"] = cp.get(camera_key,'host')
        camera["protocol"] = cp.get(camera_key,'protocol')
        camera["isNVR"] = cp.get(camera_key,'isNVR')
        camera["name"] = cp.get(camera_key,'name')
        camera["port"] = cp.get(camera_key,'port')
        camera["user"] = cp.get(camera_key,'user')
        camera["pass"] = cp.get(camera_key,'pass')
        camera["auth"] = cp.get(camera_key,'auth')
        camera["events"] = cp.get(camera_key,'events')
        cameras.append(camera)

    mqtt = {}
    mqtt["IP"] = cp.get("MQTT Broker","IP")
    mqtt["port"] = cp.get("MQTT Broker","port")
    dahua_event = DahuaEventThread(mqtt,cameras)

    dahua_event.start()

    

    