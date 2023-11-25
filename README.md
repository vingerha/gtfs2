[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)

# GTFS2
This is an adaptation of the GTFS integration in HA Core, enhancements:
- configuration via the GUI, no configuration.yaml needed
- Uses selected route to further select start/end stops
- Shows next 10 departures on the same stretch start/end , including alternative transport lines if applicable
- allows to load/update/delete datasources in gtfs2 folder
- added a sservice to update the GTFS datasource, e.g. calling the service via automation
- translations: at present only English and French

## Difference with GTFS HA core (outside of GUI setup)
Core GTFS uses start + stop, it then determines every option between them and provides the next best option, regardless of the line/route
- Pro: you receive the first applicable departure time and just have to check the type of transport (bus/tram/etc.)
- Con: you have to know exactly which start and stop you want and in the proper direction. Noting that the same stops exist with differnt ID for different routes/trips/directions

***Solution/workaround in GTFS2***: attribute added: next_departure_line shows all next departues with their line/means-of-transport. So even if you select a route first and then two stops, the attibutes will still show alternatives between those 2 stops, if applicable.

## Updates
202311DD
- realtime vehile tracking with geojson output
- workflow tweaks
- extend update service call
20231110: adding features:
- new attribute: next_departure_headsigns
- adding route shortname in selection/list to overcome data discrepancies been short name and long name
- for new datasource, allow to use a self-placed zip file in the gtfs2 folder. This for zip that are not available via URL or zip with data that may need modification to comply with extraction conditions by pygtfs
- timezone for next_departure is now used in order: agency (delivering data), if not > HA system, if not > UTC. This to resolve TZ issues for datasets without agency (timezone)

20231104: initial version

## ToDo's / In Development / Known Issues
- Issue when updating the source db, it throws a db locked error OR pygtfs. This when an existing entity for the same db starts polling it at the same time
- Issue when updating the source db: pygtfs error: at the moment unclear as errors fluctuate, posisbly a lack of resources (mem/cpu)
- bypass setup control for routes that have no trips 'today'. The configuration does a spot-check if start/end actually return data with the idea to validate the setup. However, this only checks for 'today' so if your route actually has no transport running at the day of setup (say Sunday or Holiday) then it will reject it.

## Installation via HACS :

In  HACS, select the 3-dots and then custom repositories
Add :
- URL : https://github.com/vingerha/gtfs2
- Category : Integration

In Settings > Devices & Sevices
- add the integration, note that this is GTFS2

## Configuration
Use the workflow

Example: https://github.com/vingerha/gtfs2/blob/main/example.md

## Real Time vehicle tracking

As per v1.6, the vehicle tracking output coordinates to geojson file in your www folder, which in turn can then be consumed by the geosjon integration and map card https://www.home-assistant.io/integrations/geo_json_events/
![image](https://github.com/vingerha/gtfs2/assets/44190435/a3cbea60-46f1-40e9-88c5-4b9a0519c782)


## **IMPORTANT**
- sources need to adhere to GTFS standards both for static data (zip/sqlite) as well as for real-time data (binary). 
- certain providers publish large zip-files which in turn will result in much larger db files. Unpacking may take a long time (depending HA server perf.). Example for a 117Mb zip: ~2hrs to unpack to a 7Gb sqlite
- the integration uses folder /config/gtfs2 to store the datafiles (zip and sqlite)
- the integration uses folder /config/www for geojson files, only available when using verhical tracking sources

## Data add / update
Data can be updated at your own discretion by a service, e.g. you can have a weekly automation to run the service
**Note:** for "update" to work, the name should be the ***same*** as the existing source. It will first remove the existing one and reload the one as per your URL

![image](https://github.com/vingerha/gtfs2/assets/44190435/2d639afa-376b-4956-8223-2c982dc537cb)


or via yaml

![image](https://github.com/vingerha/gtfs2/assets/44190435/0d50bb87-c081-4cd6-8dc5-9603a44c21a4)


## Thank you
- @joostlek ... massive thanks to help me through many (!) tech aspects and getting this to the inital version
- @mxbssn for initiating, bringing ideas, helping with testing
- @mark1foley for his gtfs real time integration which was enhanced with its integration in GTFS2
