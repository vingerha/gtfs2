## Example new datasource

### Get the url to the gtfs zip file
![image](https://github.com/vingerha/gtfs2/assets/44190435/d5402bb0-17a5-41e8-ac95-652ecb06bb98)

In this case that is: https://data.lemet.fr/documents/LEMET-gtfs.zip

### Add new in devices and services

![image](https://github.com/vingerha/gtfs2/assets/44190435/ed7e8fbe-c3b9-4337-982f-74da5ea6e3dd)

![image](https://github.com/vingerha/gtfs2/assets/44190435/7dd77425-07f8-45d0-8d0c-d9948fca6fbb)

![image](https://github.com/vingerha/gtfs2/assets/44190435/3688925f-63cd-451a-9db1-313a028c2188)

NOTE: this will download and unpack the zip-file to a sqlite database, which can take (many) minutes, **please be patient**

![image](https://github.com/vingerha/gtfs2/assets/44190435/02ab24ed-c10d-43e5-8c3e-f221044a1a9e)

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

## CONFIGURE Options 

After setup you can change the refresh interval and add real-time source(s)

![image](https://github.com/vingerha/gtfs2/assets/44190435/03135ba3-e9ff-4fe6-a23b-bb1f0a44c6ea)

![image](https://github.com/vingerha/gtfs2/assets/44190435/11de0f3c-ac1b-4b4d-8712-38764dfc5bd4)

![image](https://github.com/vingerha/gtfs2/assets/44190435/5895e947-882d-444e-9259-e56d7d5e426a)





Sample of the entity and its attributes
```
arrival: 2023-11-18T12:18:00+00:00
day: today
first: false
last: false
offset: 0
agency_agency_id: None
agency_agency_name: TAO (Orléans)
agency_agency_url: http://reseau-tao.fr/
agency_agency_timezone: Europe/Paris
agency_agency_lang: fr
agency_agency_phone: 0800012000
agency_agency_fare_url: None
agency_agency_email: None
origin_station_stop_id: ORLEANS:StopArea:00026500
origin_station_stop_code: None
origin_station_stop_name: Gaston Galloux
origin_station_stop_desc: None
origin_station_stop_lat: 47.884827
origin_station_stop_lon: 1.924645
origin_station_zone_id: None
origin_station_stop_url: None
origin_station_location_type: 0
origin_station_parent_station: None
origin_station_stop_timezone: Europe/Paris
origin_station_wheelchair_boarding: 0
origin_station_platform_code: None
origin_station_location_type_name: Station
origin_station_wheelchair_boarding_available: unknown
destination_station_stop_id: ORLEANS:StopArea:01001712
destination_station_stop_code: None
destination_station_stop_name: Gare d'Orléans - Quai E
destination_station_stop_desc: None
destination_station_stop_lat: 47.907085
destination_station_stop_lon: 1.90578
destination_station_zone_id: None
destination_station_stop_url: None
destination_station_location_type: 0
destination_station_parent_station: None
destination_station_stop_timezone: Europe/Paris
destination_station_wheelchair_boarding: 0
destination_station_platform_code: None
destination_station_location_type_name: Station
destination_station_wheelchair_boarding_available: unknown
route_route_id: ORLEANS:Line:40
route_agency_id: None
route_route_short_name: 40
route_route_long_name: GARE ORLEANS - PETITE MERIE
route_route_desc: None
route_route_type: 3
route_route_url: None
route_route_color: 24A472
route_route_text_color: 000000
route_type_name: Bus
trip_route_id: ORLEANS:Line:40
trip_service_id: chouette:TimeTable:4f12e6e5-93ca-4af2-b493-0858f5c73e39:LOC
trip_trip_id: ORLEANS:VehicleJourney:40_A_56_16_4002_6_124300
trip_trip_headsign: None
trip_trip_short_name: None
trip_direction_id: 0
trip_block_id: None
trip_shape_id: PME-CNY-POSC-GARE
trip_wheelchair_accessible: None
trip_bikes_allowed: None
trip_bikes_allowed_state: unknown
trip_wheelchair_access_available: unknown
origin_stop_arrival_time: 2023-11-18T12:09:05+00:00
origin_stop_departure_time: 2023-11-18T12:09:05+00:00
origin_stop_pickup_type: 0
origin_stop_sequence: 17
origin_stop_drop_off_type_state: unknown
origin_stop_pickup_type_state: Regular
origin_stop_timepoint_exact: true
destination_stop_arrival_time: 2023-11-18T12:18:00+00:00
destination_stop_departure_time: 2023-11-18T12:18:00+00:00
destination_stop_pickup_type: 0
destination_stop_sequence: 23
destination_stop_drop_off_type_state: unknown
destination_stop_pickup_type_state: Regular
destination_stop_timepoint_exact: true
next_departures: 2023-11-18T12:09:05+00:00, 2023-11-18T12:39:05+00:00, 2023-11-18T13:10:05+00:00, 2023-11-18T13:40:05+00:00, 2023-11-18T14:10:05+00:00, 2023-11-18T14:40:05+00:00, 2023-11-18T15:11:05+00:00, 2023-11-18T15:41:05+00:00, 2023-11-18T16:12:05+00:00, 2023-11-18T16:42:05+00:00
next_departures_lines: 2023-11-18T12:09:05+00:00 (GARE ORLEANS - PETITE MERIE), 2023-11-18T12:39:05+00:00 (GARE ORLEANS - PETITE MERIE), 2023-11-18T13:10:05+00:00 (GARE ORLEANS - PETITE MERIE), 2023-11-18T13:40:05+00:00 (GARE ORLEANS - PETITE MERIE), 2023-11-18T14:10:05+00:00 (GARE ORLEANS - PETITE MERIE), 2023-11-18T14:40:05+00:00 (GARE ORLEANS - PETITE MERIE), 2023-11-18T15:11:05+00:00 (GARE ORLEANS - PETITE MERIE), 2023-11-18T15:41:05+00:00 (GARE ORLEANS - PETITE MERIE), 2023-11-18T16:12:05+00:00 (GARE ORLEANS - PETITE MERIE), 2023-11-18T16:42:05+00:00 (GARE ORLEANS - PETITE MERIE)
next_departures_headsign: 2023-11-18T12:09:05+00:00 (None), 2023-11-18T12:39:05+00:00 (None), 2023-11-18T13:10:05+00:00 (None), 2023-11-18T13:40:05+00:00 (None), 2023-11-18T14:10:05+00:00 (None), 2023-11-18T14:40:05+00:00 (None), 2023-11-18T15:11:05+00:00 (None), 2023-11-18T15:41:05+00:00 (None), 2023-11-18T16:12:05+00:00 (None), 2023-11-18T16:42:05+00:00 (None)
gtfs_updated_at: 2023-11-18T11:38:52.654949+00:00
gtfs_rt_updated_at: 2023-11-18T11:40:59.832457+00:00
next_departure_realtime: 2023-11-18T12:09:30+00:00
latitude: 
longitude: 
attribution: TAO (Orléans)
device_class: timestamp
icon: mdi:bus
friendly_name: Orleans 40 outbound
```














