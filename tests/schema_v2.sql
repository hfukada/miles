CREATE TABLE activities (
            activity_id   INTEGER PRIMARY KEY,
            name          TEXT,
            sport_type    TEXT,
            -- Athlete-local wall time, stored naive (no offset/Z suffix) — see
            -- ActivityRow.start_date in this module for why.
            start_date    TEXT,
            workout_type  INTEGER,
            run_type      TEXT,
            distance_m    REAL,
            moving_time_s INTEGER,
            elapsed_time_s INTEGER,
            total_elevation_gain_m REAL,
            average_speed_mps REAL,
            max_speed_mps     REAL,
            average_heartrate REAL,
            max_heartrate     REAL,
            average_cadence   REAL,
            gear_id       TEXT,
            strava_url    TEXT,
            synced_at     TEXT,
            start_lat     REAL,
            start_lng     REAL,
            raw_json      TEXT
        , workout_label TEXT, run_type_inferred TEXT, race_effort TEXT, effort_ratio REAL, dominant_intensity TEXT, laps_synced_at TEXT, description TEXT);
CREATE TABLE area_tiles (
            area_id INTEGER NOT NULL REFERENCES areas(area_id),
            gx      INTEGER NOT NULL,
            gy      INTEGER NOT NULL,
            PRIMARY KEY (area_id, gx, gy)
        );
CREATE TABLE areas (
            area_id            INTEGER PRIMARY KEY,
            label              TEXT NOT NULL,
            center_lat         REAL NOT NULL,
            center_lng         REAL NOT NULL,
            radius_km          REAL NOT NULL,
            drive_minutes      REAL,
            elevation_provider TEXT NOT NULL DEFAULT 'terrarium',
            pinned             INTEGER NOT NULL DEFAULT 0,
            status             TEXT NOT NULL DEFAULT 'new',
            status_phase       TEXT,
            status_done        INTEGER,
            status_total       INTEGER,
            status_error       TEXT,
            created_at         TEXT NOT NULL,
            last_used_at       TEXT NOT NULL,
            imported_at        TEXT,
            region             TEXT
        );
CREATE TABLE athlete (
            id                   INTEGER PRIMARY KEY CHECK (id = 1),
            max_hr               INTEGER,
            long_run_floor_miles REAL,
            updated_at           TEXT
        );
CREATE TABLE drive_time_cache (
            origin_lat_r REAL NOT NULL,
            origin_lng_r REAL NOT NULL,
            dest_node_id INTEGER NOT NULL,
            drive_s      REAL,
            fetched_at   TEXT NOT NULL,
            PRIMARY KEY (origin_lat_r, origin_lng_r, dest_node_id)
        );
CREATE TABLE elevation_tiles (
            provider   TEXT NOT NULL,
            zoom       INTEGER NOT NULL,
            x          INTEGER NOT NULL,
            y          INTEGER NOT NULL,
            fetched_at TEXT NOT NULL,
            data       BLOB NOT NULL,
            PRIMARY KEY (provider, zoom, x, y)
        );
CREATE TABLE extract_requests (
            region       TEXT PRIMARY KEY,
            requested_at TEXT NOT NULL
        );
CREATE TABLE fitness_checkpoints (
            month         TEXT PRIMARY KEY,
            confidence    TEXT,
            source_tier   INTEGER,
            pace_5k       REAL,
            pace_10k      REAL,
            pace_half     REAL,
            pace_marathon REAL
        );
CREATE TABLE geocode_cache (
            query        TEXT PRIMARY KEY,
            lat          REAL NOT NULL,
            lng          REAL NOT NULL,
            display_name TEXT,
            fetched_at   TEXT NOT NULL
        );
CREATE TABLE geocode_misses (
            query      TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL
        );
CREATE TABLE geofabrik_index (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            fetched_at    TEXT NOT NULL,
            checked_at    TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            etag          TEXT,
            last_modified TEXT,
            data          BLOB NOT NULL
        );
CREATE TABLE gpx_files (
            file_id     INTEGER PRIMARY KEY,
            sha256      TEXT NOT NULL UNIQUE,
            filename    TEXT NOT NULL,
            creator     TEXT,
            data        BLOB NOT NULL,
            uploaded_at TEXT NOT NULL
        );
CREATE TABLE laps (
            lap_id      INTEGER PRIMARY KEY,
            activity_id INTEGER NOT NULL REFERENCES activities(activity_id) ON DELETE CASCADE,
            lap_index   INTEGER NOT NULL,
            distance_m             REAL,
            moving_time_s          INTEGER,
            elapsed_time_s         INTEGER,
            average_speed_mps      REAL,
            average_heartrate      REAL,
            max_heartrate          REAL,
            average_cadence        REAL,
            total_elevation_gain_m REAL,
            pace_zone              INTEGER,
            raw_json               TEXT, lap_type TEXT, intensity TEXT,
            UNIQUE(activity_id, lap_index)
        );
CREATE TABLE map_extracts (
            region        TEXT PRIMARY KEY,
            path          TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            fetched_at    TEXT NOT NULL,
            bounds_south  REAL,
            bounds_west   REAL,
            bounds_north  REAL,
            bounds_east   REAL,
            checked_at    TEXT,
            data_as_of    TEXT,
            etag          TEXT,
            last_modified TEXT
        );
CREATE TABLE map_tiles (
            gx            INTEGER NOT NULL,
            gy            INTEGER NOT NULL,
            query_version INTEGER NOT NULL,
            fetched_at    TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            data          BLOB NOT NULL,
            source        TEXT NOT NULL DEFAULT 'overpass', inputs TEXT,
            PRIMARY KEY (gx, gy)
        );
CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
CREATE TABLE net_candidates (
            candidate_id TEXT PRIMARY KEY,
            request_id   TEXT NOT NULL REFERENCES net_search_results(request_id) ON DELETE CASCADE,
            area_id      INTEGER NOT NULL,
            shape        TEXT NOT NULL,
            start_json   TEXT NOT NULL,
            distance_m   REAL NOT NULL,
            score_json   TEXT NOT NULL,
            missed_json  TEXT NOT NULL,
            geometry_json TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            expires_at   TEXT NOT NULL
        );
CREATE TABLE net_edges (
            edge_id                 INTEGER PRIMARY KEY,
            area_id                 INTEGER NOT NULL,
            way_id                  INTEGER NOT NULL,
            way_name                TEXT,
            highway                 TEXT NOT NULL,
            surface_class           TEXT NOT NULL,
            oneway                  TEXT NOT NULL DEFAULT 'no',
            from_node_id            INTEGER NOT NULL REFERENCES net_nodes(node_id) ON DELETE CASCADE,
            to_node_id              INTEGER NOT NULL REFERENCES net_nodes(node_id) ON DELETE CASCADE,
            length_m                REAL NOT NULL,
            has_elevation           INTEGER NOT NULL,
            gain_fwd_m              REAL,
            loss_fwd_m              REAL,
            technicality_json       TEXT NOT NULL,
            maxspeed_mph            REAL,
            maxspeed_filled         INTEGER,
            lanes                   INTEGER,
            has_separation          INTEGER,
            proximity_major_road_m  REAL,
            nearby_water_m          REAL,
            nearby_toilets_m        REAL,
            points_blob             BLOB NOT NULL
        );
CREATE TABLE net_graphs (
            area_id            INTEGER PRIMARY KEY,
            derive_version     INTEGER NOT NULL,
            built_at           TEXT NOT NULL,
            node_count         INTEGER NOT NULL,
            edge_count         INTEGER NOT NULL,
            excluded_way_count INTEGER NOT NULL
        );
CREATE TABLE net_nodes (
            node_id         INTEGER PRIMARY KEY,
            area_id         INTEGER NOT NULL,
            osm_node_id     INTEGER NOT NULL,
            kind            TEXT NOT NULL,
            name            TEXT,
            lat             REAL NOT NULL,
            lng             REAL NOT NULL,
            UNIQUE(area_id, osm_node_id)
        );
CREATE TABLE net_preferences (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            profile_json TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
CREATE TABLE net_search_results (
            request_id   TEXT PRIMARY KEY,
            area_id      INTEGER NOT NULL,
            created_at   TEXT NOT NULL,
            expires_at   TEXT NOT NULL,
            request_json TEXT NOT NULL
        );
CREATE TABLE net_way_overrides (
            way_id     INTEGER PRIMARY KEY,
            verdict    TEXT NOT NULL CHECK (verdict IN ('comfortable', 'avoid')),
            note       TEXT,
            created_at TEXT NOT NULL
        );
CREATE TABLE outbound_events (
            event_id INTEGER PRIMARY KEY,
            at       TEXT NOT NULL,
            service  TEXT NOT NULL,
            status   INTEGER NOT NULL
        );
CREATE TABLE outbound_requests (
            day     TEXT NOT NULL,
            service TEXT NOT NULL,
            host    TEXT NOT NULL,
            kind    TEXT NOT NULL DEFAULT '',
            status  TEXT NOT NULL,
            count   INTEGER NOT NULL,
            PRIMARY KEY (day, service, host, kind, status)
        );
CREATE TABLE plan_adherence (
            plan_id              INTEGER NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
            week_start           TEXT NOT NULL,
            version_n_used       INTEGER,
            actual_miles         REAL,
            actual_workouts      INTEGER,
            actual_strength_days INTEGER,
            long_run_done        INTEGER,
            mileage_ratio        REAL,
            workout_pace_delta_s REAL,
            band                 TEXT,
            flags_json           TEXT,
            PRIMARY KEY (plan_id, week_start)
        );
CREATE TABLE plan_days (
            version_id     INTEGER NOT NULL REFERENCES plan_versions(version_id) ON DELETE CASCADE,
            date           TEXT NOT NULL,
            seq            INTEGER NOT NULL DEFAULT 1,
            slot           TEXT NOT NULL,
            title          TEXT,
            target_miles   REAL,
            target_json    TEXT,
            terrain        TEXT,
            note           TEXT,
            target_minutes REAL,
            PRIMARY KEY (version_id, date, seq)
        );
CREATE TABLE plan_log (
            log_id     INTEGER PRIMARY KEY,
            plan_id    INTEGER NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
            date       TEXT NOT NULL,
            action     TEXT NOT NULL,
            reason     TEXT,
            created_at TEXT NOT NULL
        );
CREATE TABLE plan_versions (
            version_id   INTEGER PRIMARY KEY,
            plan_id      INTEGER NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
            version_n    INTEGER NOT NULL,
            created_at   TEXT NOT NULL,
            committed_at TEXT,
            note         TEXT,
            author       TEXT NOT NULL,
            UNIQUE(plan_id, version_n)
        );
CREATE TABLE plan_weeks (
            version_id              INTEGER NOT NULL REFERENCES plan_versions(version_id) ON DELETE CASCADE,
            week_start               TEXT NOT NULL,
            target_miles             REAL,
            target_miles_hi          REAL,
            target_workouts          INTEGER NOT NULL,
            target_long_run_miles    REAL,
            target_long_run_minutes  REAL,
            target_strength_days     INTEGER,
            phase                    TEXT NOT NULL,
            note                     TEXT,
            PRIMARY KEY (version_id, week_start)
        );
CREATE TABLE plans (
            plan_id         INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            race_date       TEXT NOT NULL,
            distance_bucket TEXT NOT NULL,
            goal_time_s     INTEGER,
            status          TEXT NOT NULL DEFAULT 'active',
            created_at      TEXT NOT NULL
        );
CREATE TABLE route_climbs (
            route_id  INTEGER NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
            idx       INTEGER NOT NULL,
            kind      TEXT NOT NULL,
            start_m   REAL NOT NULL,
            end_m     REAL NOT NULL,
            gain_m    REAL NOT NULL,
            avg_grade REAL NOT NULL,
            PRIMARY KEY (route_id, idx)
        );
CREATE TABLE route_grade_bands (
            route_id INTEGER NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
            band     TEXT NOT NULL,
            share    REAL NOT NULL,
            PRIMARY KEY (route_id, band)
        );
CREATE TABLE route_legs (
            route_id     INTEGER NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
            idx          INTEGER NOT NULL,
            kind         TEXT NOT NULL,
            leg_route_id INTEGER REFERENCES routes(route_id) ON DELETE SET NULL,
            from_m       REAL,
            to_m         REAL,
            distance_m   REAL,
            gain_m       REAL,
            note         TEXT,
            PRIMARY KEY (route_id, idx)
        );
CREATE TABLE route_points (
            route_id INTEGER NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
            idx      INTEGER NOT NULL,
            lat      REAL NOT NULL,
            lng      REAL NOT NULL,
            dist_m   REAL NOT NULL,
            ele_m    REAL,
            PRIMARY KEY (route_id, idx)
        );
CREATE TABLE route_tombstones (
            file_id     INTEGER NOT NULL,
            track_index INTEGER NOT NULL,
            PRIMARY KEY (file_id, track_index)
        );
CREATE TABLE route_waypoints (
            waypoint_id INTEGER PRIMARY KEY,
            route_id    INTEGER NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
            name        TEXT,
            desc        TEXT,
            lat         REAL NOT NULL,
            lng         REAL NOT NULL,
            dist_m      REAL NOT NULL,
            offset_m    REAL NOT NULL
        );
CREATE TABLE routes (
            route_id       INTEGER PRIMARY KEY,
            file_id        INTEGER NOT NULL REFERENCES gpx_files(file_id) ON DELETE CASCADE,
            track_index    INTEGER NOT NULL,
            kind           TEXT NOT NULL,
            gpx_name       TEXT,
            name           TEXT,
            source         TEXT,
            notes          TEXT,
            tags_json      TEXT NOT NULL DEFAULT '[]',
            created_at     TEXT NOT NULL,
            derive_version INTEGER,
            has_elevation  INTEGER,
            distance_m     REAL,
            gain_m         REAL,
            loss_m         REAL,
            min_ele_m      REAL,
            max_ele_m      REAL,
            ft_per_mi      REAL,
            bbox_south     REAL,
            bbox_west      REAL,
            bbox_north     REAL,
            bbox_east      REAL,
            start_lat      REAL,
            start_lng      REAL,
            end_lat        REAL,
            end_lng        REAL,
            shape          TEXT, elevation_source TEXT NOT NULL DEFAULT 'auto', ele_source TEXT, elevation_pending INTEGER NOT NULL DEFAULT 0,
            UNIQUE(file_id, track_index)
        );
CREATE TABLE terrain_backfill_failures (
            activity_id   INTEGER PRIMARY KEY REFERENCES activities(activity_id) ON DELETE CASCADE,
            status_code   INTEGER,
            error         TEXT NOT NULL,
            failed_at     TEXT NOT NULL,
            model_version TEXT
        );
CREATE TABLE terrain_backfill_queue (
            activity_id INTEGER PRIMARY KEY REFERENCES activities(activity_id) ON DELETE CASCADE,
            start_date  TEXT,
            queued_at   TEXT NOT NULL
        );
CREATE TABLE terrain_backfill_status (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            status      TEXT NOT NULL DEFAULT 'idle',
            phase       TEXT,
            done        INTEGER,
            total       INTEGER,
            started_at  TEXT,
            finished_at TEXT,
            error       TEXT
        );
CREATE TABLE terrain_segment_dates (
            activity_id   INTEGER PRIMARY KEY REFERENCES activities(activity_id) ON DELETE CASCADE,
            start_date    TEXT NOT NULL,
            segment_count INTEGER NOT NULL DEFAULT 1,
            needs_dem     INTEGER NOT NULL DEFAULT 0
        );
CREATE TABLE terrain_segments (
            activity_id INTEGER NOT NULL REFERENCES activities(activity_id) ON DELETE CASCADE,
            idx         INTEGER NOT NULL,
            dist_m      REAL NOT NULL,
            elapsed_s   INTEGER,
            grade_dpct  INTEGER NOT NULL,
            speed_cms   INTEGER,
            heartrate   INTEGER,
            cadence_spm INTEGER,
            is_walking  INTEGER NOT NULL,
            temp_dc     INTEGER,
            ele_source  INTEGER NOT NULL,
            PRIMARY KEY (activity_id, idx)
        ) WITHOUT ROWID;
CREATE TABLE terrain_settings (
            id                   INTEGER PRIMARY KEY CHECK (id = 1),
            walk_threshold_spm   REAL NOT NULL,
            rolling_window_days  INTEGER NOT NULL
        );
CREATE TABLE terrain_snapshots (
            snapshot_id               INTEGER PRIMARY KEY,
            as_of                     TEXT NOT NULL,
            window_days               INTEGER NOT NULL,
            walk_onset_grade_pct      REAL NOT NULL,
            descent_comfort_grade_pct REAL NOT NULL,
            confidence                TEXT NOT NULL,
            bins_json                 TEXT NOT NULL,
            created_at                TEXT NOT NULL,
            UNIQUE (as_of, window_days)
        );
CREATE TABLE weather (
            activity_id        INTEGER PRIMARY KEY REFERENCES activities(activity_id) ON DELETE CASCADE,
            fetched_at         TEXT NOT NULL,
            temp_c_start       REAL,
            temp_c_end         REAL,
            temp_c_avg         REAL,
            temp_c_max         REAL,
            apparent_temp_c_max REAL,
            humidity_avg       REAL,
            precip_mm          REAL,
            wind_kph_avg       REAL,
            hourly_json        TEXT,
            raw_json           TEXT
        );
CREATE INDEX idx_drive_time_cache_fetched ON drive_time_cache(fetched_at);
CREATE INDEX idx_net_candidates_request ON net_candidates(request_id);
CREATE INDEX idx_net_edges_area ON net_edges(area_id);
CREATE INDEX idx_net_edges_way ON net_edges(way_id);
CREATE INDEX idx_net_nodes_area ON net_nodes(area_id);
CREATE INDEX idx_outbound_events_at ON outbound_events(at);
CREATE INDEX idx_terrain_segment_dates_date ON terrain_segment_dates(start_date);
CREATE INDEX idx_terrain_segments_grade ON terrain_segments(grade_dpct);
