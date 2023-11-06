[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)

# GTFS2
This is an adaptation of the GTFS integration in HA Core, enhancements:
- configuration via the GUI, no configuration.yaml needed
- Uses selected route to further select start/end stops
- Shows next 10 departures on the same stretch start/end , including alternative transport lines if applicable
- allows to load/update/delete datasources in gtfs2 folder
- added a sservice to update the GTFS datasource, e.g. calling the service via automation
- translations: at present only English and French

<h4> Note: 
- certain sources provide large zip-files, which in turn will result in much larger db files. Unpacking may take a long time (depending HA server perf.). Example for a 117Mb zip: ~2hrs to unpack to a 7Gb sqlite
- the integration uses folder /config/gtfs2 to store the data (zip and sqlite)</h4>

## Difference with GTFS HA core (outside of GUI setup)
Core GTFS uses start + stop, it then determines every option between them and provides the next best option, regardless of the line/route
- Pro: you receive the first applicable departure time and just have to check the type of transport (bus/tram/etc.)
- Con: you have to know exactly which start and stop you want and in the proper direction. Noting that the same stops exist with differnt ID for different routes/trips/directions

***Solution/workaround in GTFS2***: attribute added: next_departure_line shows all next departues with their line/means-of-transport. So even if you select a route first and then two stops, the attibutes will still show alternatives between those 2 stops, if applicable.

## Updates
- 20231104: initial version

## ToDo's
- Issue when updating the source db, it throws a db locked error. This when an existing entity for the same db starts polling it at the same time
- Icon for the integration (brands)
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

**IMPORTANT**
- when setting up a new datasource (by using the url to the external ZIP file) this may easily take 5-10 minutes before the next step is reached, depending on the content of the ZIP.

## Data add / update
Data can be updated at your own discretion by a service, e.g. you can have a weekly automation to run the service
**Note:** for "update" to work, the name should be the ***same*** as the existing source. It will first remove the existing one and reload the one as per your URL

![image](https://github.com/vingerha/gtfs2/assets/44190435/2defc23d-a1a0-40be-b610-6c5360fbd464)

or via yaml

![image](https://github.com/vingerha/gtfs2/assets/44190435/2fea7926-a64d-43b6-a653-c95f1f01c66d)




## Thank you
- @joostlek ... massive thanks to help me through many (!) tech aspects and getting this to the inital version
- @mxbssn for initiating, bringing ideas, helping with testing



