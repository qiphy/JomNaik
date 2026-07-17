# JomNaik

JomNaik is a Klang Valley multimodal transit app. It combines map search,
OpenTripPlanner routing, rail/BRT/bus schedules, RapidKL live vehicle
estimates, covered-walkway guidance, KTM Komuter fares, e-hailing fallback,
and optional community incident reporting.

## Architecture

- **Flutter app**: map, search, itinerary, GPS, Profile, Supabase sign-in and
  incident reporting.
- **FastAPI backend**: place search, OTP orchestration, fares, GTFS-Realtime
  estimates, traffic adjustment, incident-aware ranking and stop data.
- **OpenTripPlanner (OTP)**: multimodal routing over the prebuilt `graph.obj`.
- **Static GTFS**: Rapid Rail/BRT, Rapid Bus, MRT feeder and KTM Komuter.
- **Supabase**: authentication, anonymous station presence and incident reports.

## Prerequisites

- Flutter SDK and Android Studio/device emulator
- Python 3.11+
- Java 17+
- At least 4 GB available memory for OTP

The repository already includes the OTP JAR, GTFS archives and `graph.obj`.

## Local setup

### 1. Configure the backend

```bash
cd jomnaik_backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create `jomnaik_backend/.env` with values appropriate for your environment:

```dotenv
TOMTOM_API_KEY=your_tomtom_key
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=server_only_service_role_key
SUPABASE_PRESENCE_TABLE=anonymous_station_presence
SUPABASE_INCIDENT_REPORTS_TABLE=anonymous_incident_reports
```

`SUPABASE_SERVICE_ROLE_KEY` must remain on the backend only. Do not put it in
the Flutter app or commit it to source control.

### 2. Start OTP

Open a terminal:

```bash
cd jomnaik_backend
java -Xmx3G -jar otp-2.4.0-shaded.jar --load .
```

OTP listens on `http://localhost:8080` by default and loads the local
`graph.obj`.

### 3. Start the FastAPI backend

Open a second terminal:

```bash
cd jomnaik_backend
source venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Check that it is running:

```bash
curl http://localhost:8000/api/health
```

### 4. Configure Supabase

Run these SQL files in the Supabase SQL Editor if you use the corresponding
features:

- `jomnaik_backend/supabase_incident_reports.sql` — registered-user incident
  reports with anonymous report contents.
- Create `anonymous_station_presence` if you want opt-in station-presence
  signals for congestion scoring.

The Flutter app uses the Supabase project URL and publishable key defined in
`lib/main.dart`, or values supplied at build time.

### 5. Run the Flutter app

```bash
cd jomnaik
flutter pub get
```

For an Android emulator, use `10.0.2.2` to reach the host machine's backend:

```bash
flutter run --dart-define=BACKEND_URL=http://10.0.2.2:8000
```

For a physical device, replace the address with your computer's LAN IP, for
example:

```bash
flutter run --dart-define=BACKEND_URL=http://192.168.1.10:8000
```

Ensure the phone and computer are on the same network and that the firewall
allows port `8000`.

## Rebuilding the OTP graph

Rebuild only after changing GTFS data, station transfers, frequency rules or
the OSM pedestrian network:

```bash
cd jomnaik_backend
./rebuild_otp_sheltered_graph.sh
```

The script generates an updated `graph.obj`. Restart OTP after a rebuild.

## Testing and validation

```bash
cd jomnaik_backend
venv/bin/python -m py_compile server.py

cd ../jomnaik
flutter analyze lib/main.dart
```

## How Codex accelerated the workflow

Codex acted as a full-stack development partner across the Flutter app,
FastAPI service, GTFS feeds, OTP graph and Supabase integration.

- **Fast diagnosis**: traced routing issues across stop coordinates, transfer
  links, OTP configuration, service frequencies and deployment settings.
- **Transit-data engineering**: filtered GTFS services, added transfer rules,
  generated MRT feeder frequency patterns, rebuilt the graph and integrated
  KTM fare data.
- **Feature delivery**: implemented search, map pins, covered-walkway labels,
  live vehicle estimates, Profile authentication, station congestion, incident
  reports and incident-aware alternative routing.
- **Practical verification**: ran syntax/analyzer checks and used graph-build
  output to validate the data pipeline after changes.

This reduced the time spent switching between mobile UI work, routing logic,
data preparation and backend integration, while keeping the app focused on
Klang Valley public transport use cases.
