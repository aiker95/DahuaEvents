"""
Connect Dahua events to mqtt (and vice versa)

Borrowed code from
    https://github.com/psyciknz/CameraEvents,
    https://github.com/johnnyletrois/dahua-watch
    https://github.com/SaWey/home-assistant-dahua-event
Author: Raainman

Changes so far:
Base version is from CameraEvents
Changed the base version to python:3.8-slim-buster (Debian) because I could not even perfom nano when I logged into the docker sh
Removed all the image processing (snapshots), because I got errors that base64 encoding should not be done in json. Have to figure out how to resolve this.

ToDo:
Change json message for VideoMotion message, include the RegionName.
    VideoMotion;action = Start;index = 0;data = {
       "Id" : [ 0 ],
       "RegionName" : [ "Oprit" ],
       "SmartMotionEnable" : true
    }
    VideoMotion;action = Stop;index = 0;data = {
      "Id" : [ 0 ],
       "RegionName" : [ "Oprit" ],
       "SmartMotionEnable" : true
    }
Change CameraEvents --> DahuaEvents
Find out how to send images
Also define mqtt messages to update the camera (perform PTZ for instance)
Split up the code

VideoMotion,CrossLineDetection,AlarmLocal,VideoLoss,VideoBlind
...

"""


REQUIREMENTS = ['pycurl>=7']

import threading
import requests
import datetime
import re
try:
    #python 3+
    from configparser import ConfigParser
except:
    # Python 2.7
    from ConfigParser import ConfigParser
import logging
import os
import socket
import pycurl
import json
import time
import paho.mqtt.client as paho   # pip install paho-mqtt
import base64

version = "0.1.3"

mqttc = paho.Client("CameraEvents-" + socket.gethostname(), clean_session = True)

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)

# create formatter
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# add formatter to ch
ch.setFormatter(formatter)
_LOGGER.addHandler(ch)



def setup(config):
    """Set up Dahua event listener."""
    #config = config.get(DOMAIN)

    dahua_event = DahuaEventThread(
        None,
        None
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
    SNAPSHOT_TEMPLATE = "{protocol}://{host}:{port}/cgi-bin/snapshot.cgi?channel={channel}"
    #SNAPSHOT_TEMPLATE = "{protocol}://{host}:{port}/cgi-bin/snapshot.cgi?1"
    #SNAPSHOT_TEMPLATE = "{protocol}://{host}:{port}/cgi-bin/snapshot.cgi?chn={channel}"
    SNAPSHOT_EVENT = "{protocol}://{host}:{port}/cgi-bin/eventManager.cgi?action = attachFileProc&Flags[0]=Event&Events=%5B{events}%5D"
    #cgi-bin/snapManager.cgi?action = attachFileProc&Flags[0]=Event&Events=[VideoMotion%2CVideoLoss]




    def __init__(self,  name, device_cfg, client, basetopic):
        if device_cfg["channels"]:
            self.channels = device_cfg["channels"]
        else:
            self.channels = {}
        self.Name = name
        self.CurlObj = None
        self.Connected = None
        self.Reconnect = None
        self.MQTTConnected = None
        self.user = device_cfg.get("user")
        self.password = device_cfg.get("pass")
        self.auth = device_cfg.get("auth")
        self.mqtt = device_cfg.get("mqtt")
        self.protocol  = device_cfg.get("protocol")
        self.host = device_cfg.get("host")
        self.port = device_cfg.get("port")
        self.alerts = device_cfg.get("alerts")
        self.client = client
        self.basetopic = basetopic
        self.snapshotoffset = device_cfg.get("snapshotoffset")

        #generate the event url
        self.url = self.EVENT_TEMPLATE.format(
            protocol = self.protocol,
            host = self.host,
            port = self.port,
            events = device_cfg.get("events")
        )


        self.isNVR = False
        try:
            # Get NVR parm, to get channel names if NVR
            self.isNVR = device_cfg.get("isNVR")

            if self.isNVR:
                #generate the channel url
                self.channelurl  = self.CHANNEL_TEMPLATE.format(
                    protocol = device_cfg.get("protocol"),
                    host = device_cfg.get("host"),
                    port = device_cfg.get("port")
               )
                self.snapshotevents = self.SNAPSHOT_EVENT.format(
                    protocol = self.protocol,
                    host = self.host,
                    port = self.port,
                    events = device_cfg.get("events")
               )

                #RPCConnect(1)

                # get channel names here
                #table.ChannelTitle[0].Name = Garage
                _LOGGER.debug("Device " + name + " Getting channel ids: " + self.channelurl)
                response = requests.get(self.channelurl, auth = requests.auth.HTTPDigestAuth(self.user, self.password))
                for line in response.text.splitlines():
                    match = re.search(r'.\[(?P<index>[0-4])\]\..+\=(?P<channel>.+)', line)
                    if match:
                        _index = int(match.group("index"))
                        _channel = match.group("channel")
                        self.channels[_index] = _channel
            else:
                self.channels[0] = self.Name

            _LOGGER.info("Created Data Device: " + name)

        except Exception as e:
            _LOGGER.debug("Device " + name + " is not an NVR: " + str(e))
            _LOGGER.debug("Device " + name + " is not an NVR")


    def channelIsMine(self, channelname = "", channelid = -1):
        for channel in self.channels:
            if channelname is not None and channelname == self.channels[channel]:
                return channel
            elif channelid > -1 and channel == channelid:
                return channel

        return -1

    def SnapshotImage(self, channel, channelName, message):
        imageurl  = self.SNAPSHOT_TEMPLATE.format(
                host = self.host, 
                protocol = self.protocol, 
                port  = self.port, 
                channel = channel
           )
        image = None
        _LOGGER.info("Snapshot Url: " + imageurl)
        try:
            if self.auth == "digest":
                image = requests.get(imageurl, stream = True, auth = requests.auth.HTTPDigestAuth(self.user, self.password)).content
            else:
                image = requests.get(imageurl, stream = True, auth = requests.auth.HTTPBasicAuth(self.user, self.password)).content

            if image is not None and len(image) > 0:
                #construct image payload
                #{{ \"message\": \"Motion Detected: {0}\", \"imagebase64\": \"{1}\" }}"
                imgpayload = base64.encodestring(image)
                msgpayload = json.dumps({"message":message, "imagebase64":imgpayload})
                #msgpayload = "{{ \"message\": \"{0}\", \"imagebase64\": \"{1}\" }}".format(message, imgpayload)

                self.client.publish(self.basetopic +"/{0}/Image".format(channelName), msgpayload)
        except Exception as ex:
            _LOGGER.error("Error sending image: " + str(ex))
            try:
                imagepayload = ""
                with open("default.png", 'rb') as thefile:
                    imagepayload = thefile.read().encode("base64")
                msgpayload = json.dumps({"message":"ERR:" + message, "imagebase64": imagepayload})
                self.client.publish(self.basetopic +"/{0}/Image".format(channelName), msgpayload)
            except:
                pass

    # Connected to camera
    def OnConnect(self):
        _LOGGER.debug("[{0}] OnConnect()".format(self.Name))
        self.Connected = True

    #disconnected from camera
    def OnDisconnect(self, reason):
        _LOGGER.debug("[{0}] OnDisconnect({1})".format(self.Name, reason))
        self.Connected = False

    #on receive data from camera.
    def OnReceive(self, data):
        #self.client.loop_forever()
        Data = data.decode("utf-8", errors = "ignore")
        #_LOGGER.debug("[{0}]: {1}".format(self.Name, Data))

        crossData = ""

        for Line in Data.split("\r\n"):
            if Line == "HTTP/1.1 200 OK":
                self.OnConnect()

            if not Line.startswith("Code="):
                continue
            #_LOGGER.info("Received: "+ Line)

            Alarm = dict()
            Alarm["name"] = self.Name
            for KeyValue in Line.split(';'):
                Key, Value = KeyValue.split('=')
                Alarm[Key] = Value

            index =  int(Alarm["index"]        )
            if index in self.channels:
                Alarm["channel"] = self.channels[index]
            else:
                Alarm["channel"] = self.Name + ":" + str(index)

            if Alarm["Code"] == "VideoMotion":
                VideoMotionData = json.loads(Alarm["data"])
                #Can there be more than one Region ??
                RegionName = ', '.join(VideoMotionData["RegionName"])
                _LOGGER.info("Video Motion received: "+ Alarm["action"] + Alarm["name"] + " Index: " + Alarm["channel"] + " Code: " + Alarm["Code"] + " RegionName: " + RegionName)
                if Alarm["action"] == "Start":
                    if not self.client.connected_flag:
                        self.client.reconnect()
                    self.client.publish(self.basetopic +"/" + Alarm["Code"] + "/" + Alarm["channel"] + "/" + RegionName, "ON")
                else:
                    self.client.publish(self.basetopic +"/" + Alarm["Code"] + "/" + Alarm["channel"] + "/" + RegionName, "OFF")
            elif Alarm["Code"] ==  "CrossRegionDetection" or Alarm["Code"] == "CrossLineDetection":
                if Alarm["action"] == "Start" or Alarm["action"] == "Stop" :
                    try:
                        #_LOGGER.info(Alarm["Code"] + Alarm["action"] + " received: " + Alarm["data"])
                        crossData = json.loads(Alarm["data"])
                        if "Direction" not in crossData:
                            direction = "unknown"
                        else:
                            direction = crossData["Direction"]

                        region = crossData["Name"]
                        object = crossData["Object"]["ObjectType"]
                    except Exception as ivsExcept:
                        _LOGGER.error("Error getting IVS data: " + str(ivsExcept))
                    payload = { 'Code':Alarm["Code"],'Direction':direction,'Region':region,'ObjectType':object,'Action':Alarm["action"] }
                    if not self.client.connected_flag:
                        self.client.reconnect()
                    self.client.publish(self.basetopic +"/IVS/" + Alarm["channel"],payload=json.dumps(payload))
            elif Alarm["Code"] == "FaceDetection":
                if Alarm["action"] == "Start" or Alarm["action"] == "Stop" :
                    try:
                        _LOGGER.info(Alarm["Code"] + " " + Alarm["action"] + " received: " + Alarm["data"])
                        crossData = json.loads(Alarm["data"])
                        object = crossData["Object"]["ObjectType"]
                    except Exception as ivsExcept:
                        _LOGGER.error("Error getting IVS data: " + str(ivsExcept))
                    payload = { 'Code':Alarm["Code"],'ObjectType':object,'Action':Alarm["action"] }
                    if not self.client.connected_flag:
                        self.client.reconnect()
                    self.client.publish(self.basetopic +"/IVS/FaceDetection/" + Alarm["channel"],payload=json.dumps(payload))
            elif Alarm["Code"] == "NewFile":
                if Alarm["action"] == "Pulse":
                    try:
                        #_LOGGER.info(Alarm["Code"] + " " + Alarm["action"] + " received: " + Alarm["data"])
                        fileData = json.loads(Alarm["data"])
                        file = fileData["File"].replace( '\\' , '' )
                        _LOGGER.info("NewFile:["+file+"]")
                        fileext = file[-3:]
                        filesize = fileData["Size"]
                        storagepoint = fileData["StoragePoint"]
                        #Try to find [A-Z] in filename, this has some meaning like M=Motion, R=Record, F=???
                        if re.search("\[[A-Z]\]",file):
                            filecode = re.findall("[A-Z]",re.findall("\[[A-Z]\]",file)[0])[0]
                        else:
                            filecode = '?'
                        if re.search("\[[0-9]\]",file):
                            filesequence = re.findall("[0-9]",re.findall("\[[0-9]\]",file)[0])[0]
                        else:
                            filesequence = '0'
                    except Exception as ivsExcept:
                        _LOGGER.error("Error getting NewFile data: " + str(ivsExcept))
                    payload = { 'Code':Alarm["Code"],'File':file,'Extension':fileext,'Size':filesize, 'StoragePoint':storagepoint, 'Filecode':filecode, 'Filesequence':filesequence }
                    if not self.client.connected_flag:
                        self.client.reconnect()
                    self.client.publish(self.basetopic +"/NewFile/" + Alarm["channel"] + "/"+fileext,payload=json.dumps(payload))
            else:
                _LOGGER.info("dahua_event_received: "+  Alarm["name"] + " Index: " + Alarm["channel"] + " Code: " + Alarm["Code"])
                _LOGGER.info("dahua_event_received Line: "+ Line)
                if not self.client.connected_flag:
                    self.client.reconnectreconnect()
                self.client.publish(self.basetopic +"/" + Alarm["channel"] + "/" + Alarm["name"], Alarm["Code"])

class DahuaEventThread(threading.Thread):
    """Connects to device and subscribes to events"""
    Devices = []
    NumActivePlayers = 0

    CurlMultiObj = pycurl.CurlMulti()
    NumCurlObjs = 0


    def __init__(self,  mqtt, cameras):
        """Construct a thread listening for events."""

        self.basetopic = mqtt["basetopic"]

        self.client = paho.Client("CameraEvents-" + socket.gethostname(), clean_session = True)
        self.client.on_connect = self.mqtt_on_connect
        self.client.on_disconnect = self.mqtt_on_disconnect
        self.client.message_callback_add(self.basetopic +"/picture", self.mqtt_on_picture_message)
        self.client.message_callback_add(self.basetopic +"/alerts", self.mqtt_on_alert_message)

        self.client.will_set(self.basetopic +"/$online", False, qos = 0, retain = True)

        self.alerts = True

        for device_cfg in cameras:

            device = DahuaDevice(device_cfg.get("name"), device_cfg, self.client, self.basetopic)
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

        #connect to mqtt broker

        _LOGGER.debug("Connecting to MQTT Broker")
        self.client.connect(mqtt["IP"], int(mqtt["port"]), 60)

        _LOGGER.debug("Starting MQTT Loop")
        self.client.loop_start()

        threading.Thread.__init__(self)
        self.stopped = threading.Event()


    def run(self):
        heartbeat = 0
        """Fetch events"""
        while 1:
            Ret, NumHandles = self.CurlMultiObj.perform()
            if Ret != pycurl.E_CALL_MULTI_PERFORM:
                break

        Ret = self.CurlMultiObj.select(1.0)
        while not self.stopped.isSet():
            # Sleeps to ease load on processor
            time.sleep(.05)
            heartbeat = heartbeat + 1
            if heartbeat % 1000 == 0:
                _LOGGER.debug("Heartbeat: " + str(datetime.datetime.now()))
                if not self.client.connected_flag:
                    self.client.reconnect()
                self.client.publish(self.basetopic +"/$heartbeat", str(datetime.datetime.now()))

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

    def mqtt_on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            _LOGGER.info("Connected to MQTT OK Returned code = {0}".format(rc))
            self.client.connected_flag = True
            self.client.publish(self.basetopic +"/$online", True, qos = 0, retain = True)
            self.client.publish(self.basetopic +"/$version", version)
            if self.alerts:
                state = "ON"
            else:
                state = "OFF"

            for device in self.Devices:
                device.alerts = state
                self.client.publish(self.basetopic +"/" + device.Name + "/alerts/state", state)
            #self.client.subscribe(self.basetopic +"/#")
            #self.client.subscribe("CameraEventsPy/alerts")

        else:
            _LOGGER.info("Camera : {0}: Bad mqtt connection Returned code = {1}".format("self.Name", rc))
            self.client.connected_flag = False

    def mqtt_on_disconnect(self, client, userdata, rc):
        logging.info("disconnecting reason  "  +str(rc))
        self.client.connected_flag = False


    def mqtt_on_picture_message(self, client, userdata, msg):

        #if msg.payload.decode() == "Hello world!":
        _LOGGER.info("Picture Msg Received: Topic:{0} Payload:{1}".format(msg.topic, msg.payload))
        msgchannel = msg.topic.split("/")[1]
        for device in self.Devices:
            channel = device.channelIsMine(msgchannel)
            if channel > -1:
                _LOGGER.debug("Found Camera: {0} channel: {1}: Name:{2}".format(device.Name, channel, device.channels[channel]))
                #device.SnapshotImage(channel+device.snapshotoffset, msgchannel, "Snap Shot Image")
                break


    def mqtt_on_alert_message(self, client, userdata, msg):
        if msg.payload == 'ON':
            newState = True
        else:
            newState = False

        deviceName = msg.topic.split('/')[1]
        _LOGGER.info("Camera: {0}: Msg Received: Topic:{1} Payload:{2}".format(deviceName, msg.topic, msg.payload))
        for device in self.Devices:
            #channel = self.Devices[device].channelIsMine("Garage")
            if device.Name == deviceName:
                device.alerts = newState
                _LOGGER.info("Turning Alerts {0}".format(newState))
                self.client.publish(self.basetopic +"/" + device.Name + "/alerts/state", msg.payload)

    def mqtt_on_cross_message(self, client, userdata, msg):
        if msg.payload == 'ON':
            newState = True
        else:
            newState = False

        deviceName = msg.topic.split('/')[1]
        _LOGGER.info("Camera: {0}: Msg Received: Topic:{1} Payload:{2}".format(deviceName, msg.topic, msg.payload))
        for device in self.Devices:
            #channel = self.Devices[device].channelIsMine("Garage")
            if device.Name == deviceName:
                device.alerts = newState
                _LOGGER.info("Turning Alerts {0}".format(newState))
                self.client.publish(self.basetopic +"/" + device.Name + "/alerts/state", msg.payload)

if __name__ == '__main__':

    cameras = []
    cp = ConfigParser()
    _LOGGER.info("Loading config")
    filename = {"config.ini", "conf/config.ini"}
    dataset = cp.read(filename)

    try:
        if len(dataset) != 1:
            raise ValueError("Failed to open/find all files")
        camera_items = cp.items("Cameras")
        for key, camera_key in camera_items:
            #do something with path
            camera_cp = cp.items(camera_key)
            camera = {}
            #temp = cp.get(camera_key, "host")
            camera["host"] = cp.get(camera_key, 'host')
            camera["protocol"] = cp.get(camera_key, 'protocol')
            camera["isNVR"] = cp.get(camera_key, 'isNVR')
            camera["name"] = cp.get(camera_key, 'name')
            camera["port"] = cp.getint(camera_key, 'port')
            camera["user"] = cp.get(camera_key, 'user')
            camera["pass"] = cp.get(camera_key, 'pass')
            camera["auth"] = cp.get(camera_key, 'auth')
            camera["events"] = cp.get(camera_key, 'events')
            channels = {}
            if cp.has_option(camera_key, 'channels'):
                try:
                    channellist = cp.get(camera_key, 'channels').split('|')
                    for channel in channellist:
                        channelIndex = channel.split(':')[0]
                        channelName = channel.split(':')[1]
                        channels[int(channelIndex)] = channelName

                except Exception as e:
                    _LOGGER.warning("Warning, No channel list in config (may be obtained from NVR):" + str(e))
                    channels = {}

            # added new snapshot offset section.
            if cp.has_option(camera_key, 'snapshotoffset'):
                camera["snapshotoffset"] = cp.getint(camera_key, 'snapshotoffset')
            else:
                camera["snapshotoffset"] = 0
            camera["channels"] = channels
            cameras.append(camera)

        mqtt = {}
        mqtt["IP"] = cp.get("MQTT Broker", "IP")
        mqtt["port"] = cp.get("MQTT Broker", "port")
        mqtt["basetopic"] = cp.get("MQTT Broker", "BaseTopic")
        dahua_event = DahuaEventThread(mqtt, cameras)

        dahua_event.start()
    except Exception as ex:
        _LOGGER.error("Error starting:" + str(ex))
