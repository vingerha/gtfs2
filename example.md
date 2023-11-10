## Example new datasource

### Get the url to the gtfs zip file
![image](https://github.com/vingerha/gtfs2/assets/44190435/d5402bb0-17a5-41e8-ac95-652ecb06bb98)

In this case that is: https://data.lemet.fr/documents/LEMET-gtfs.zip

### Add new in devices and services

![image](https://github.com/vingerha/gtfs2/assets/44190435/ed7e8fbe-c3b9-4337-982f-74da5ea6e3dd)

![image](https://github.com/vingerha/gtfs2/assets/44190435/7dd77425-07f8-45d0-8d0c-d9948fca6fbb)

![image](https://github.com/vingerha/gtfs2/assets/44190435/3688925f-63cd-451a-9db1-313a028c2188)

NOTE: this will download and unpack the zip-file to a sqlite database, which can take time (examples from 10mins to 2hrs), **please be patient**

![image](https://github.com/vingerha/gtfs2/assets/44190435/dd26f517-1cd9-4386-b4ea-c605d02a0ac7)


## Select the route

After the datasource is retrieved, you can select the route that you'd like to track on 
By selecting a route and its direction, it allows to filter the proper stops

![image](https://github.com/vingerha/gtfs2/assets/44190435/80d133c5-b00e-43f8-aef0-c203eba4eb6b)

## Select the start / end stop

By default it will show the start and end stop of the chosen route
Name: select a name that applies to your route, the name is used also for updates
Offset: allows to add an offset in minutes to the collection of data, e.g. if you need 10minutes to walk to the stop, you are not interested in any departure earlier than 10 minutes from now
Refresh interval: reloads the data with the new timings, if your transport has a high frequency, you may want to reduce this

![image](https://github.com/vingerha/gtfs2/assets/44190435/8007911c-f1c7-406c-9295-4d132df07ab6)

## Added

You can add a optional area

![image](https://github.com/vingerha/gtfs2/assets/44190435/f2f855f9-bc07-405d-8b0b-09b3da7e4f79)

Sample of the entity and its attributes
```
arrival: "2023-11-04T09:42:29+00:00"
day: today
first: false
last: false
offset: 0
agency_agency_id: LE MET
agency_agency_name: LE MET'
agency_agency_url: https://lemet.fr
agency_agency_timezone: Europe/Paris
agency_agency_lang: FR
agency_agency_phone: 0.800.00.29.38
agency_agency_fare_url: https://services.lemet.fr/fr/billetterie
agency_agency_email: contact@lemet.fr
origin_station_stop_id: "6010"
origin_station_stop_code: None
origin_station_stop_name: P+R WOIPPY
origin_station_stop_desc: None
origin_station_stop_lat: "49.150349"
origin_station_stop_lon: "6.173323"
origin_station_zone_id: None
origin_station_stop_url: https://services.lemet.fr/fr/biv/arret/1627
origin_station_location_type: "0"
origin_station_parent_station: None
origin_station_stop_timezone: None
origin_station_wheelchair_boarding: "1"
origin_station_platform_code: None
origin_station_location_type_name: Station
origin_station_wheelchair_boarding_available: true
destination_station_stop_id: "6180"
destination_station_stop_code: None
destination_station_stop_name: FELIX ALCAN
destination_station_stop_desc: None
destination_station_stop_lat: "49.112572"
destination_station_stop_lon: "6.199158"
destination_station_zone_id: None
destination_station_stop_url: https://services.lemet.fr/fr/biv/arret/7324
destination_station_location_type: "0"
destination_station_parent_station: None
destination_station_stop_timezone: None
destination_station_wheelchair_boarding: "1"
destination_station_platform_code: None
destination_station_location_type_name: Station
destination_station_wheelchair_boarding_available: true
route_route_id: A-98
route_agency_id: LE MET
route_route_short_name: MA
route_route_long_name: METTIS A
route_route_desc: None
route_route_type: "3"
route_route_url: None
route_route_color: F0980C
route_route_text_color: FFFFFF
route_type_name: Bus
trip_route_id: A-98
trip_service_id: HIV2324-Sam_Sp23-Samedi-21
trip_trip_id: 1281546-HIV2324-Sam_Sp23-Samedi-21
trip_trip_headsign: MA - BORNY
trip_trip_short_name: None
trip_direction_id: "0"
trip_block_id: "196205"
trip_shape_id: A0014
trip_wheelchair_accessible: "1"
trip_bikes_allowed: "2"
trip_bikes_allowed_state: false
trip_wheelchair_access_available: true
origin_stop_arrival_time: "2023-11-04 10:16:00"
origin_stop_departure_time: "2023-11-04 10:16:00"
origin_stop_drop_off_type: 0
origin_stop_pickup_type: 0
origin_stop_sequence: 1
origin_stop_drop_off_type_state: Regular
origin_stop_pickup_type_state: Regular
origin_stop_timepoint_exact: true
destination_stop_arrival_time: "2023-11-04 10:42:29"
destination_stop_departure_time: "2023-11-04 10:42:29"
destination_stop_drop_off_type: 0
destination_stop_pickup_type: 0
destination_stop_sequence: 19
destination_stop_drop_off_type_state: Regular
destination_stop_pickup_type_state: Regular
destination_stop_timepoint_exact: true
next_departures:
  - "2023-11-04 10:16:00"
  - "2023-11-04 10:31:00"
  - "2023-11-04 10:46:00"
  - "2023-11-04 11:01:00"
  - "2023-11-04 11:16:00"
  - "2023-11-04 11:31:00"
  - "2023-11-04 11:46:00"
  - "2023-11-04 12:01:00"
  - "2023-11-04 12:16:00"
  - "2023-11-04 12:31:00"
next_departures_lines:
  - 2023-11-04 10:16:00 (METTIS A)
  - 2023-11-04 10:31:00 (METTIS A)
  - 2023-11-04 10:46:00 (METTIS A)
  - 2023-11-04 11:01:00 (METTIS A)
  - 2023-11-04 11:16:00 (METTIS A)
  - 2023-11-04 11:31:00 (METTIS A)
  - 2023-11-04 11:46:00 (METTIS A)
  - 2023-11-04 12:01:00 (METTIS A)
  - 2023-11-04 12:16:00 (METTIS A)
  - 2023-11-04 12:31:00 (METTIS A)
updated_at: "2023-11-04T10:07:07.085514"
attribution: LE MET'
device_class: timestamp
icon: mdi:bus
friendly_name: MyRouteInMetz
```














