[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)

# GTFS2
Copy from HA GTFS, aiming to improve the integration

<h4> Note: uses folder /config/gtfs2 </h4>

## Updates
- 20221212: version de base, copy/fork et adaptation au prix instantanés

## Installation depuis HACS :

Dans HACS, cliquer sur ... puis depots personnalisés
Ajouter :
- URL : https://github.com/vingerha/gtfs2
- Catégorie : Intégration

## Configuration
Exemples de configuration :
```
  - platform: gtfs2
    origin: "STOPPOINT:00812"
    destination: "STOPPOINT:01549"
    name: "Ligne 530 (test)"
    data: zou.zip
    include_tomorrow: true
```
