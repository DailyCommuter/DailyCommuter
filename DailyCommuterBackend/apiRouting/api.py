import config
import os
import time
from datetime import datetime
import json
import requests
import sqlite3
from dotenv import load_dotenv
from google.transit import gtfs_realtime_pb2
from flask import jsonify
from DailyCommuterBackend.db import get_db
from DailyCommuterBackend.models import Route
import firebase_admin
from firebase_admin import credentials, auth


# API key must be given to server when deploying
load_dotenv()
BUS_FEED_KEY = os.getenv("BUS_FEED_KEY")
TRANSIT_TOKEN = os.getenv("TRANSIT_TOKEN")

# cred = credentials.Certificate("path/to/serviceAccountKey.json")
# firebase_admin.initialize_app(cred)

# Verify Firebase ID token
def verify_token(id_token):
    try:
        decoded_token = auth.verify_id_token(id_token)
        return decoded_token['uid']
    except Exception as e:
        print("Token verification failed:", e)
        return None
    

# URLs for all api calls
train_update_urls = [
    config.ACESR_FEED_URL,
    config.BDFM_FEED_URL,
    config.G_FEED_URL,
    config.NQRW_FEED_URL,
    config.L_FEED_URL,
    config.NUMBERS_AND_S_FEED_URL,
    config.SIR_FEED_URL,]

service_alert_urls = [
    config.ALL_SERVICE_ALERTS_URL_GTFS,
    config.SUBWAY_ALERTS_URL_GTFS,
    config.BUS_ALERTS_URL_GTFS,
    config.LIRR_ALERTS_URL_GTFS,
    config.METRO_NORTH_ALERTS_URL_GTFS]

elev_escal_json_urls = [
    config.ELEV_ESCAL_CURRENT_OUTAGES_JSON,
    config.ELEV_ESCAL_UPCOMING_OUTAGES_JSON,
    config.ELEV_ESCAL_EQUIPMENTS_OUTAGES_JSON,]


def fetch_data(endpoint, key=None):
    feed = gtfs_realtime_pb2.FeedMessage()
    if key is None:
        response = requests.get(endpoint)
        if not response.status_code == 200:
            print(f"Failed to fetch data: {response.status_code}")
            return
        feed.ParseFromString(response.content)
    else:
        response = requests.get(f"{endpoint}key={key}")
        feed.ParseFromString(response.content)
    return feed


# Train update GTFS structure is as follows:
'''
Has 2 types of entities:
trip_update (if there is a delay etc) show information about the stops a train will make in the future (stopTimeUpdates)
Contains:
    trip
    stop_time_update (for each stop on the route_id)
vehicle (what stop it's currently at on this route_id) show information about the current status of the train
Contains:
    trip
    timestamp
    stop_id

Example:
----------------------------------
id: "000031FS"
trip_update {
    trip {
        trip_id: "111150_FS.S01R"
        start_time: "18:31:30"
        start_date: "20250324"
        route_id: "FS"
    }
    stop_time_update {
        arrival {
            time: 1742855490
        }
        departure {
            time: 1742855490
        }
        stop_id: "S01S"
    }

    ...

    stop_time_update {
        arrival {
            time: 1742855880
        }
        departure {
            time: 1742855880
        }
        stop_id: "D26S"
    }
} 
----------------------------------
id: "000030FS"
vehicle {
    trip {
        trip_id: "111100_FS.N01R"
        start_time: "18:31:00"
        start_date: "20250324"
        route_id: "FS"
    }
    timestamp: 1742855460
    stop_id: "D26N"
}
----------------------------------
'''
def update_trains(feed):
    try:
        with get_db() as db:
            trip_update_id = None
            for entity in feed.entity:
                # Check if update_id already exists (prevents duplicates)
                id_exists = db.execute(
                    "SELECT id FROM trip_update WHERE update_id = ?", 
                    (entity.id,)
                ).fetchone()

                if id_exists:
                    # print(f"Skipping duplicate update_id: {entity.id}")
                    continue
                
                # Get and store the trip info about the following updates
                if entity.HasField('trip_update'):
                    db.execute(
                        """
                        INSERT INTO trip_update 
                        (update_id, trip_id, start_tm, start_dt, route_id)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (entity.id,
                        entity.trip_update.trip.trip_id, 
                        entity.trip_update.trip.start_time, 
                        entity.trip_update.trip.start_date, 
                        entity.trip_update.trip.route_id,)
                    )

                    # Get id from the main trip_update table as reference 
                    # for stop_update and vehicle_update tables
                    trip_update_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

                    # Get and store the actual updates info
                    for update in entity.trip_update.stop_time_update:
                        if trip_update_id is not None:
                            db.execute(
                                """
                                INSERT INTO stop_update 
                                (trip_update_id, arrival, departure, stop_id, direction)
                                VALUES (?, ?, ?, ?, ?)
                                """,
                                (trip_update_id,
                                update.arrival.time, 
                                update.departure.time, 
                                update.stop_id[:-1],
                                update.stop_id[-1],)
                            )

                # Get and store the vehecle info about the above updates
                if entity.HasField('vehicle'):
                    if trip_update_id is not None:
                        db.execute(
                            """
                            INSERT INTO vehicle_update
                            (trip_update_id, timestmp, curr_stop_id)
                            VALUES (?, ?, ?)
                            """,
                            (trip_update_id,
                            entity.vehicle.timestamp, 
                            entity.vehicle.stop_id,)
                        )
            db.commit()

    except sqlite3.IntegrityError as e:
        print(f"Integrity Error: {e}")
    except Exception as e:
        print(f"Error updating database: {e}")
    finally:
        pass


### ALERTING LOGIC ###
# at certain time/interval, get all alerts
# check if any of the alerts are related to any of the stops on a given route
# recalculate the route
#   if route ends up being longer (by certain time amount?) then push notif
#   else, dont do anything

# Delete all of the previous updates
# Alerts give info that affect either a specific stop or an entire route
# Get current updates and store the stop_id or route_id as well as the actual alert
# Service alert GTFS structure is as follows:
'''
id: "A28S#EL226"
alert {
  active_period {
    start: 1747360800
    end: 1759204800
  }
  informed_entity {
    stop_id: "A28S"
  }
  header_text {
    translation {
      text: "Elevator outage @ 34 St-Penn Station: uptown C/E platform to lower mezzanine for access to Penn Station concourse and rest of complex [Capital Replacement]"
      language: "en"
    }
  }
  description_text {
    translation {
      text: "Elevator outage @ 34 St-Penn Station: uptown C/E platform to lower mezzanine for access to Penn Station concourse and rest of complex [Capital Replacement]"
      language: "en"
    }
  }
}

Or sometimes can be convoluted to be:
----------------------------------
id: "lmm:planned_work:19829"
alert {
  active_period {
    start: 1727755260
    end: 1727776800
  }
  ...
  ...
  ...
  active_period {
    start: 1758254460
    end: 1758276000
  }
  informed_entity {
    agency_id: "MTASBWY"
    route_id: "GS"
  }
  header_text {
    translation {
      text: "Take the [7] instead"
      language: "en"
    }
    translation {
      text: "<p>Take the [7] instead</p>"
      language: "en-html"
    agency_id: "MTASBWY"
    route_id: "GS"
  }
  header_text {
    translation {
      text: "Take the [7] instead"
      language: "en"
    }
    translation {
      text: "<p>Take the [7] instead</p>"
      language: "en-html"
    route_id: "GS"
  }
  header_text {
    translation {
      text: "Take the [7] instead"
      language: "en"
    }
    translation {
      text: "<p>Take the [7] instead</p>"
      language: "en-html"
  header_text {
    translation {
      text: "Take the [7] instead"
      language: "en"
    }
    translation {
      text: "<p>Take the [7] instead</p>"
      language: "en-html"
      text: "Take the [7] instead"
      language: "en"
    }
    translation {
      text: "<p>Take the [7] instead</p>"
      language: "en-html"
    translation {
      text: "<p>Take the [7] instead</p>"
      language: "en-html"
    }
  }
      language: "en-html"
    }
  }
  description_text {
    }
  }
  description_text {
  description_text {
    translation {
    translation {
      text: "[S] 42 St Shuttle operates daily during days and evenings.\n\nPlan your trip at mta.info or download the MTA app for iOS or Android.."
      text: "[S] 42 St Shuttle operates daily during days and evenings.\n\nPlan your trip at mta.info or download the MTA app for iOS or Android.."
      language: "en"
      language: "en"
    }
    }
    translation {
      text: "<p>[S] 42 St Shuttle operates daily during days and evenings.</p><p></p><p>Plan your trip at <a title=\"\" href=\"https://mta.info\" rel=\"noopener noreferrer nofollow\" data-link-auto=\"\" target=\"_blank\">mta.info</a> or download the MTA app for <a title=\"\" href=\"https://apps.apple.com/us/app/mymta/id1297605670\" rel=\"noopener noreferrer nofollow\" target=\"_blank\">iOS</a> or <a title=\"\" href=\"https://play.google.com/store/apps/details?id=info.mta.mymta&amp;hl=en_US&amp;gl=US\" rel=\"noopener noreferrer nofollow\" target=\"_blank\">Android</a>..</p>"
      language: "en-html"
    }
  }
}
'''
def update_subway_alerts():
    feed = fetch_data(endpoint = config.SUBWAY_ALERTS_URL_GTFS)
    db = get_db()
    try:
        db.execute('DELETE FROM my_data;')
        for entity in feed.entity:
            db.execute(
                'INSERT INTO subway_alerts (alert_id)'
                'VALUES (?)',
                (entity.id,)
            )
            inf_ent = entity.alert.informed_entity
            for ie in inf_ent:
                if ie.HasField('stop_id'):
                    db.execute(
                        'INSERT INTO subway_alerts (stop_id, alert_text)'
                        'VALUES (?, ?)',
                        (ie.stop_id, entity.alert.header_text.translation[0].text,)
                    )
                elif ie.HasField('route_id'):
                    db.execute(
                        'INSERT INTO subway_alerts (route_id, alert_text)'
                        'VALUES (?, ?)',
                        (ie.route_id, entity.alert.header_text.translation[0].text,)
                    )
        db.commit()

    except sqlite3.IntegrityError as e:
        print(f"Integrity Error: {e}")
    except Exception as e:
        print(f"Error updating database: {e}")


def update_subway_feeds():
    # Update all the trains
    for url in train_update_urls:
        update_trains(fetch_data(url))


def geocoder(address):
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        'q': address,
        'format': 'json',
        'limit': 1
    }
    headers = {
        'User-Agent': 'DailyCommuter chz9577@nyu.edu'
    }
    response = requests.get(url, params=params, headers=headers)
    data = response.json()
    if data:
        lat = float(data[0]['lat'])
        lon = float(data[0]['lon'])
        return lat, lon
    else:
        raise ValueError("Could not geocode address", address)


def createRoute(start_address, end_address, arriveby, userid):
    start_lat, start_lon = geocoder(start_address)
    time.sleep(1) #prevent going over API limit
    end_lat, end_lon = geocoder(end_address)

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute('''
                        INSERT INTO routes (start_address, end_address, start_lat, start_lon, end_lat, end_lon, arrival_time, userid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (start_address, end_address, start_lat, start_lon, end_lat, end_lon, arriveby, userid)) 
            route_id = c.lastrowid
            conn.commit()
            print("after commit")
    except sqlite3.IntegrityError as e:
        print(f"Integrity Error: {e}")
    except Exception as e:
        print(f"Error updating database: {e}")
    finally:
        return getRoute(route_id)


def getRoute(route_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT routeid, start_address, end_address,
               start_lat, start_lon, end_lat, end_lon,
               arrival_time, userid
        FROM routes
        WHERE routeid = ?
    ''', (route_id,))

    row = c.fetchone()
    if row:
        return Route(*row)
    else:
        return None


def Router(route):
    url = "https://external.transitapp.com/v3/otp/plan"
    headers = {
        "apiKey": TRANSIT_TOKEN
    }
    params = {
        'fromPlace': f"{route.start_lat},{route.start_lon}",
        'toPlace': f"{route.end_lat},{route.end_lon}",
        'arriveBy': 'true',
        'time': route.arrival_time,
        'date': datetime.today().strftime("%Y-%m-%d")
    }


    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        r1 =  data['plan']['itineraries'][0]
        duration = r1['duration']
        route.estimateTime = duration
        stops = []
        #iterate through the first itenarary - pull out the start/from from the route object
        #then add in all stops, to route between
        #type of stop, 0 is start, 1 is intermediate, 2 is end
        # Handle the very first point (from[0])
        # Handle the first "from" only once at the very beginning
        first_leg = r1['legs'][0]
        stops.append({
            'lat': first_leg['from']['lat'],
            'lon': first_leg['from']['lon'],
            'name': first_leg['from'].get('name', 'Start'),
            'type': 0  # Start
        })

        # Now go through each leg
        for i, leg in enumerate(r1['legs']):
            # Add any intermediate stops
            for stop in leg.get('intermediateStops', []):
                stops.append({
                    'lat': stop['lat'],
                    'lon': stop['lon'],
                    'name': stop.get('name', f'Intermediate {i}'),
                    'type': 1  # Intermediate
                })

            # Add the leg's "to" location
            stops.append({
                'lat': leg['to']['lat'],
                'lon': leg['to']['lon'],
                'name': leg['to'].get('name', f'Stop {i}'),
                'type': 2 if i == len(r1['legs']) - 1 else 1  # Mark as end if it's the last leg
            })
        conn = get_db()
        c = conn.cursor()
        for stop in stops:
            c.execute('''
                INSERT INTO points (routeid, lat, lon, name, type)
                VALUES (?, ?, ?, ?, ?)
            ''', (route.id, stop['lat'], stop['lon'], stop.get('name', ''), stop['type']))
        conn.commit()

        with open('test_route_response.json', 'w') as f:
            json.dump(data, f, indent=2)
        print("✅ Saved response to test_route_response.json", flush=True)
        return jsonify(response.json())
    except requests.exceptions.RequestException as e:
        print("❌ Request Error:", e, flush=True)
        return jsonify({"error": str(e)}), 500



# Get a SINGLE saved route for a given user and route_name
# @param userid: user id of requester
# @param route_name: route name of requested route
# @return planed route
def get_and_plan_route(userid, route_name):
    try:
        with get_db() as db:
            route_id = db.execute('''
                SELECT routeid
                FROM routes
                WHERE userid, route_name = ?, ?
                ''', 
                (userid, route_name,)).fetchall()
            
            route = getRoute(route_id)

    except sqlite3.IntegrityError as e:
        print(f"Integrity Error: {e}")
    except Exception as e:
        print(f"Error updating database: {e}")
    finally:
        return Router(route)


# Get ALL the saved routes for a given user
# @param userid: user id of requester
# @return dictionary: {route_name : {start_address, end_address, arrival_time}, {...}}
'''
{“School”:
  {
    “start_address” : start_address,
    “end_address” : end_address,
    “arrival_time” : arrival_time
   },
 “Work”:
    {
        “start_address” : start_address,
        “end_address” : end_address,
        “arrival_time” : arrival_time
    },
 ...}
'''
def get_saved_routes(userid):
    try:
        with get_db() as db:
            routes = db.execute('''
                SELECT routeid, route_name
                FROM routes
                WHERE userid = ?
                ''', 
                (userid,)).fetchall()
            saved_routes = []
            for route in routes:
                cur_route = getRoute(route["routeid"])
                details = {"start_address" : f"{cur_route.start_address}",
                           "end_address" : f"{cur_route.end_address}",
                           "arrival_time" : f"{cur_route.arrival_time}",}
                saved_routes.append({f"{route["route_name"]}" : details})

    except sqlite3.IntegrityError as e:
        print(f"Integrity Error: {e}")
    except Exception as e:
        print(f"Error updating database: {e}")
    finally:
        return saved_routes


# Gets all the subway stops for NYC using the transitapp api and saves them in the db
# Should be called at app startup and possibly some other times (maybe after loading a certain page?)
def save_all_subway_stops():
    url = "https://external.transitapp.com/v3/public/stops_for_network"
    headers = {
        "apiKey": TRANSIT_TOKEN
    }
    params = {
        'network_id': "NYC Subway|NYC"
    }
    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        stoplist = response.json()
    except requests.exceptions.RequestException as e:
        print("Request Error:", e, flush=True)
        return jsonify({"error": str(e)}), 500

    try:
        with get_db() as db:
            for stop in stoplist["stops"]:
                db.execute('''  
                    INSERT INTO subway_stops (global_stop_id, parent_station_global_stop_id, route_type, rt_stop_id, stop_lat, stop_lon, stop_name, wheelchair_boarding)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (stop["global_stop_id"], 
                          stop["parent_station_global_stop_id"], 
                          stop["route_type"], 
                          stop["rt_stop_id"], 
                          stop["stop_lat"], 
                          stop["stop_lon"], 
                          stop["stop_name"], 
                          stop["wheelchair_boarding"],))
    except sqlite3.IntegrityError as e:
        print(f"Integrity Error: {e}")
    except Exception as e:
        print(f"Error updating database: {e}")


# This uses the public api from photon
# We should eventually transition this to our own installation of the api
#   since we dont want to overload the public api
# Source is https://photon.komoot.io/, https://github.com/komoot/photon
'''
Returns this kind of data:
{
  "features": [
    {
      "geometry": {
        "coordinates": [
          -73.95657522344695,
          40.7691872
        ],
        "type": "Point"
      },
      "type": "Feature",
      "properties": {
        "osm_id": 266873992,
        "extent": [
          -73.9567115,
          40.769335,
          -73.9564424,
          40.7690481
        ],
        "country": "United States",
        "city": "New York",
        "countrycode": "US",
        "postcode": "10021",
        "locality": "Lenox Hill",
        "type": "house",
        "osm_type": "W",
        "osm_key": "building",
        "housenumber": "319",
        "street": "East 73rd Street",
        "district": "Manhattan",
        "osm_value": "apartments",
        "state": "New York"
      }
    },
    {...},
    {...}
    ]
}
'''
def address_autocomplete(input_text):
    url = "https://photon.komoot.io/api/?"
    params = {
        'q' : input_text,
        'lat': "40.741975",     # location bias to Geographic Center of NYC
        'lon': "-73.907326",
        'limit': 5,             # limit 5 most relevant results
        'lang' : "en",
        'layer' : "house"       # filter by building address layer first
    }
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        locations = response.json()
    except requests.exceptions.RequestException as e:
        print("Request Error:", e, flush=True)
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        print(f"Error with address_autocomplete: {e}")
    finally:
        result = []
        for i in range(len(locations["features"])):
            address = locations["features"][i]["properties"]["housenumber"],
            street = locations["features"][i]["properties"]["street"],
            city = locations["features"][i]["properties"]["city"],
            zipcode = locations["features"][i]["properties"]["postcode"]
            # state = locations["features"][i]["properties"]["state"],
            # country = locations["features"][i]["properties"]["countrycode"]
            result.append(f"{address} {street} {zipcode} {city}")
        return locations
