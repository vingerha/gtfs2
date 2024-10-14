name: Bug report
about: Please first check the [wiki-pages](https://github.com/vingerha/gtfs2/wiki/6.-Issues,-challenges-&-workarounds) before raising a ticket
title: "[Bug]: "
labels: ["bug", "triage"]
assignees:
  - []
body:
  - type: markdown
    attributes:
      value: |
        Note that clairvoyancy does not exist ... no details = no response
  - type: textarea
    id: what-happened
    attributes:
      label: A clear and concise description of what the bug is.
      description: |
        - has it worked before
        - what it does not do vs. expectation
        - have you verified if the source/provider is working correctly, usually they have their own website.
      render: shell
  - type: textarea
    id: sources
    attributes:
      label: Provide details on the data source(s)
      description: |
        - url to the zip file of the **static datasource**
        - have you updated the datasource to the latest version, some of them update on a weekly basis
        - if (!) realtime issue of if(!) position issue: url to its source
      render: shell
  - type: textarea
    id: startstop_based
    attributes:
      label: Config in case of setup using start / stop station
      description: |
        - route ID
        - stop ID
        - outward/return
      render: shell
  - type: textarea
    id: location_based
    attributes:
      label: Config in case of setup using start / stop station
      description: |
        - location lat/lon used
        - which transport types are missing or incorrect
      render: shell  
  - type: textarea
    id: release
    attributes:
      label: Info on your installed version
      description: |
        - gtfs2 release 
        - HA type (HAOS/Container)
      render: shell    
  - type: textarea
    id: logs
    attributes:
      label: Relevant log output
      description: Please copy and paste any relevant log output. This will be automatically formatted into code, so no need for backticks.
      render: shell



