[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)

# GTFS2
This is an adaptation of the GTFS integration in HA Core, enhancements:
- configuration via the GUI, no configuration.yaml needed
- Uses selected route to further select start/end stops
- Shows next 10 departures on the same stretch start/end , including alternative transport lines if applicable
- allows to load/update/delete datasources in gtfs2 folder
- added a sservice to update the GTFS datasource, e.g. calling the service via automation

<h4> Note: uses folder /config/gtfs2 to store the data (zip and sqlite)</h4>

## Updates
- 20231104: initial version

## ToDo's
- resolve issue that when updating the gtfs sqlite, it may throw a sqlite db locked error when an existing entity for the same db starts polling it

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
It requires the following structure
```
service: gtfs2.update_gtfs
data:
  name: ... name-of-your-source
  url: https://....url-to-your-zip
```
**Note:** for "update" to work, the name should be the ***same*** as the existing source. It will first remove the existing one and reload the one as per your URL

![image](https://github.com/vingerha/gtfs2/assets/44190435/2defc23d-a1a0-40be-b610-6c5360fbd464)


## Thank you
- @joostlek ... massive thanks to help me through many tech aspects of getting to the inital version
- @mxbssn for initiating, bringing ideas, helping with testing



