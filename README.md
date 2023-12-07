![GitHub release (with filter)](https://img.shields.io/github/v/release/vingerha/gtfs2) ![GitHub](https://img.shields.io/github/license/vingerha/gtfs2) [![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)

# GTFS2 for Static and RealTime Public transport status collecting
This is an adaptation of the GTFS integration in HomeAssistant Core, enhancements:
- configuration via the GUI, no configuration.yaml needed
- Uses a route to further select start/end stops
- Shows next 10 departures on the same stretch start/end , including alternative transport lines if applicable
- allows to load/update/delete datasources in gtfs2 folder from the GUI
- Option to add gtfs realtime source/url
- Option to add gtfs realtime vehicle location source/url, generates geojson file which can be used for tracking vehicle on map card
- Option to add gtfs realtime alerts source/url
- A service to update the GTFS static datasource, e.g. for calling the service via automation
- translations: English and French 

## Updates info
v0.1.9, Finetuning
- reduce log-spamming messages

v0.1.8 Add gtfs alerts handling

v0.1.6, stabilizing
- realtime vehile tracking with geojson output
- workflow tweaks
- extend update service call
- increase stability with reboots, loss of data(source)

v0.1.5, adding features:
- new attribute: next_departure_headsigns
- adding route shortname in selection/list to overcome data discrepancies been short name and long name
- for new datasource, allow to use a self-placed zip file in the gtfs2 folder. This for zip that are not available via URL or zip with data that may need modification to comply with extraction conditions by pygtfs
- timezone for next_departure is now used in order: agency (delivering data), if not > HA system, if not > UTC. This to resolve TZ issues for datasets without agency (timezone)

v0.1.0: initial version

## ToDo's / In Development / Known Issues

- Extraction of static data throws error, due to sometimes very long extraction time. Error can be ignored but should be avoided.

## Installation via HACS :

1. In  HACS, select the 3-dots and then custom repositories, add :
- URL : https://github.com/vingerha/gtfs2
- Category : Integration

2. In Settings > Devices & Sevices
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
=======
## Known issues/challenges with source data

Static gtfs:
- not complying to the pygtfs unpacking library, examples: missing dates in feed_info > manual fix
- calendar not showing if a service is run on a specific day > fix via adding calendar_dates to filter, only works if (!) calendar_dates is used alternatively for the same purpose
- missing routes/stops/times, transport runs but gtfs does nto show it > report issue with your gtfs data provider
- routes show A > B (outward) but stop selection shows inversed B > A, within one gtfs source both good as incorrect start/end can show up  > report issue with your gtfs data provider

Realtime gtfs
- only a few realtime providers also add vehicle positions with lat/lon, these are not always up to date > report issue with your gtfs data provider
- format incorrect of incomming json/feed > report issue with your gtfs data provider, they should adhere to standards
- realtime data not always available, few refreshes are fine then nothing then fine again, often related to timeout from provider > report issue with your gtfs data provider

## Thank you
- @joostlek ... massive thanks to help me through many (!) tech aspects and getting this to the inital version
- @mxbssn for initiating, bringing ideas, helping with testing
- @Pulpyyyy for testing, ideas
- @mark1foley for his gtfs real time integration which was enhanced with its integration in GTFS2

