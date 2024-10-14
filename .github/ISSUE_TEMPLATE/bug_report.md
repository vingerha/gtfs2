---
name: Bug report
about: Please first check the [wiki-pages](https://github.com/vingerha/gtfs2/wiki/6.-Issues,-challenges-&-workarounds) before raising a ticket
title: '[BUG]: '
labels: ''
assignees: ''
---

Note that clairvoyancy does not exist ... no details = no response

1. A clear and concise description of what the bug is.
- has it worked before
- what it does not do vs. expectation
- have you verified if the source/provider is working correctly, usually they have their own website.
  
2. Datasource(s).
- url to the zip file of the **static datasource**
- have you updated the datasource to the latest version, some of them update on a weekly basis
- if (!) realtime issue of if(!) position issue: url to its source

3. In case of setup using start / stop station
- route ID
- stop ID
- outward/return

4. In case of location based setup
- location lat/lon used
- which transport types are missing or incorrect

**Release used**
- gtfs2 release 
- HA type (HAOS/Container)

**Additional**
Please add debug logs
- remove the configuration
- setup to 'debug' logging, either via the integration (needs at least 1 config to be OK) or in configuration.yaml (requires a restart)
```
logger:
  default: warning
  logs:
    custom_components.gtfs2: debug
```
- reconfigure your failing setup
- extract the logs that cover the time of the issue and attach
- switch logging back to normal when no longer needed
