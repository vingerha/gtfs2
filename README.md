[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)

# GTFS2
Copy from HA GTFS, aiming to improve the integration

<h4> Note: uses folder /config/gtfs2 to store the data (zip and sqlite)</h4>

## Updates
- 20231102: major update, moved away from configiuration.yaml setup 
- 20231025: adds attributes 'next_departures' with max (!) 10 values of the remaining departure times for that day (and first of tomorrow if configured)

## Installation via HACS :

In  HACS, select the 3-dots and then custom repositories
Add :
- URL : https://github.com/vingerha/gtfs2
- Category : Integration

In Settings > Devices & Sevices
- add the integration, note that this is GTFS2

## Configuration
Use the workflow
**IMPORTANT**
- when setting up a new workflow, by using the url to the external ZIP file, this may easily take 5-10 minutes before the next step is reached, depending on the content of the ZIP.
```
## How to find origin/destination
Note that the format of these can be different for each (!) source.
- Option 1: open the zip-file and search in stops.txt
- Option 2: wait for the setup after the initial integration, in /config/gtfs2 open the sqlite db (e.g. DBBrowser for sqlite) and search through the stops table....this is easier to use but requires to know how to browse db's
