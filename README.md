![GitHub release (with filter)](https://img.shields.io/github/v/release/vingerha/gtfs2) ![GitHub](https://img.shields.io/github/license/vingerha/gtfs2) [![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)

# GTFS2 for Static and RealTime Public transport status collecting
This is an adaptation of the GTFS integration in HomeAssistant Core, enhancements:
- configuration via the GUI, no configuration.yaml needed
- Uses a route to further select start/end stops
- Shows next 10 departures on the same stretch start/end , including alternative transport lines if applicable
- allows to load/update/delete datasources in gtfs2 folder from the GUI
- Option to add gtfs **realtime trip updates** source/url
- Option to add gtfs **realtime vehicle location** source/url, generates geojson file which can be used for tracking vehicle on map card
- Option to add gtfs **realtime alerts** source/url
- A service to update the GTFS static datasource, e.g. for calling the service via automation
- translations: English and French 

**[Documentation](https://github.com/vingerha/gtfs2/wiki)**

