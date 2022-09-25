"""Parse timetable from GTFS files"""
from __future__ import annotations

import argparse
import calendar as cal
import itertools
import json
import math
import os
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import List, Iterable, Any, NamedTuple, Tuple, Callable, Dict, TypeVar

import numpy as np
import pandas as pd
import pathos.pools as p
from loguru import logger
from pathos.helpers.pp_helper import ApplyResult
from pathos.helpers import cpu_count

from pyraptor.timetable.io import write_timetable
from pyraptor.model.timetable import (
    RaptorTimetable,
    Stop,
    Stops,
    Trip,
    Trips,
    TripStopTime,
    TripStopTimes,
    Station,
    Stations,
    Transfer,
    Transfers,
    TimetableInfo,
    RouteInfo,
    Routes,
    Coordinates,
    TransportType,
)
from pyraptor.model.shared_mobility import SharedMobilityFeed, public_transport_stop, shared_mobility_stops, \
    RaptorTimetableSM
from pyraptor.util import mkdir_if_not_exists, str2sec, MIN_DIST


@dataclass
class GtfsTimetable(TimetableInfo):
    """Gtfs Timetable data"""

    trips: pd.DataFrame = None
    calendar: pd.DataFrame = None
    stop_times: pd.DataFrame = None
    stops: pd.DataFrame = None
    routes: pd.DataFrame = None
    transfers: pd.DataFrame = None


TIMETABLE_FILENAME = "timetable"
SHARED_MOB_TIMETABLE_FILENAME = "timetable_sm"


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-i",
        "--input",
        type=str,
        default="data/input/NL-timetable",
        help="Input directory",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="data/output",
        help="Input directory",
    )
    parser.add_argument(
        "-d",
        "--date",
        type=str,
        default="20210906",
        help="Departure date (yyyymmdd)"
    )
    parser.add_argument(
        "-a",
        "--agencies",
        nargs="+",
        default=[],
        help="Names of the agencies whose services are included in the timetable "
             "(refer to the agency_name field in the agency.txt table). "
             "If nothing is specified, all the agencies are included by default."
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=cpu_count(),
        help="Number of jobs to run (greater than 0, leave default to auto-detect available cpus)"
    )
    parser.add_argument(
        "-sm",
        "--shared_mobility",
        type=bool,
        action=argparse.BooleanOptionalAction,   # --shared_mobility    evaluates True,
                                                 # --no-shared_mobility evaluates False
        default=False,
        help="If True, shared-mobility data are included",
    )
    parser.add_argument(
        "-f",
        "--feeds",
        type=str,
        default="data/input/gbfs.json",
        help="Path to .json key specifying list of feeds and langs"
    )

    arguments = parser.parse_args()
    return arguments


# TODO allow to specify timetable name - as of now, it is hardcoded as either "timetable.pcl" or "timetable_sm.pcl"
def generate_timetable(
        input_folder: str,
        output_folder: str,
        departure_date: str,
        agencies: List[str],
        shared_mobility: bool,
        feeds_path: str,
        n_jobs: int
):
    """Main function"""

    logger.debug("Input directory                   : {}", input_folder)
    logger.debug("Output directory                  : {}", output_folder)
    logger.debug("Departure date                    : {}", departure_date)
    logger.debug("Agencies                          : {}", agencies)
    logger.debug("Using shared-mobility             : {}", shared_mobility)

    if shared_mobility:
        logger.debug("Path to shared-mobility feeds     : {}", feeds_path)

    logger.debug("jobs                              : {}", n_jobs)

    logger.info("Parse timetable from GTFS files")
    mkdir_if_not_exists(output_folder)

    gtfs_timetable: GtfsTimetable = read_gtfs_timetable(input_folder, departure_date, agencies)
    timetable: RaptorTimetable = gtfs_to_pyraptor_timetable(gtfs_timetable, n_jobs)

    if shared_mobility:
        timetable: RaptorTimetableSM = add_shared_mobility_to_pyraptor_timetable(timetable, feeds_path, n_jobs)

    timetable.counts()

    # This is so there is no need to generate the timetable each time
    #  we want/do not want sm data
    if shared_mobility:
        timetable_name = SHARED_MOB_TIMETABLE_FILENAME
    else:
        timetable_name = TIMETABLE_FILENAME
    write_timetable(output_folder=output_folder, timetable=timetable, timetable_name=timetable_name)


class CalendarHandler:
    """
    Class that handles the processing of the calendar and calendar_dates tables
    of the provided GTFS feed.
    """

    def __init__(self, input_folder: str | bytes | os.PathLike):
        """
        :param input_folder: path to the folder containing the calendar
            and/or calendar_dates tables
        """

        calendar_path = os.path.join(input_folder, "calendar.txt")
        if os.path.exists(calendar_path):
            self.calendar: pd.DataFrame = pd.read_csv(calendar_path, dtype={"start_date": str, "end_date": str})
        else:
            self.calendar = None

        calendar_dates_path = os.path.join(input_folder, "calendar_dates.txt")
        if os.path.exists(calendar_dates_path):
            self.calendar_dates: pd.DataFrame = pd.read_csv(calendar_dates_path, dtype={"date": str})
        else:
            self.calendar_dates = None

    def get_active_service_ids(self, on_date: str, valid_service_ids: Iterable) -> Iterable:
        """
        Returns the list of service ids active in the provided date. Said service ids
        are extracted from the calendar and calendar_dates tables.

        :param on_date: date to get the active services for
        :param valid_service_ids: list of service ids considered valid.
            Any service_id outside this list will not be considered in the calculations.
        :return: list of service ids active in the provided date
        """

        if self._is_recommended_calendar_repr():
            return self._handle_recommended_calendar(on_date, valid_service_ids)
        elif self._is_alternate_calendar_repr():
            return self._handle_alternate_calendar(on_date, valid_service_ids)
        else:
            raise Exception("Unhandled Calendar Representation")

    def _is_recommended_calendar_repr(self) -> bool:
        """
        Returns true if the service calendar is represented in the recommended way:
        calendar table for regular services, calendar dates table for exceptions.
        :return:
        """

        # If calendar table is present and not empty, the service calendar
        # is in the recommended way
        return self.calendar is not None and len(self.calendar) > 0

    def _handle_recommended_calendar(self, date: str, valid_service_ids: Iterable) -> pd.Series:
        """
        Returns the list of service ids active in the provided date, only if those ids
        are included in the provided valid service ids list.

        :param date:
        :param valid_service_ids:
        :return:
        """

        # Consider only valid service_ids
        only_valid_services = self.calendar[self.calendar["service_id"].isin(valid_service_ids)]

        def is_service_active_on_date(row):
            # Check if date is included in service interval
            date_format = "%Y%m%d"
            start_date = datetime.strptime(row["start_date"], date_format)
            end_date = datetime.strptime(row["end_date"], date_format)
            date_to_check = datetime.strptime(date, date_format)
            is_in_service_interval = start_date <= date_to_check <= end_date

            # Check if the service is active in the weekday of the provided date
            weekday_to_col = {
                0: "monday",
                1: "tuesday",
                2: "wednesday",
                3: "thursday",
                4: "friday",
                5: "saturday",
                6: "sunday",
            }
            weekday = cal.weekday(date_to_check.year, date_to_check.month, date_to_check.day)
            is_weekday_active = row[weekday_to_col[weekday]] == 1

            exception_type = self._get_exception_for_service_date(row["service_id"], date)

            # Service is normally active and no exception on that day
            is_normally_active = is_in_service_interval and is_weekday_active and exception_type == -1

            # Service is active because of an exceptional date
            is_exceptionally_active = not (is_in_service_interval and is_weekday_active) and exception_type == 1

            return is_normally_active or is_exceptionally_active

        # Extract only the rows of the services active on the provided date
        active_on_date_mask = only_valid_services.apply(is_service_active_on_date, axis="columns")

        services_active_on_date = only_valid_services[active_on_date_mask]

        return services_active_on_date["service_id"]

    def _is_alternate_calendar_repr(self):
        """
        Returns true if the service calendar is represented in the alternate way:
        just the calendar dates table where each record represents a service day
        :return:
        """

        # If the only calendar table is calendar_dates, then this is the
        # alternate way of representing the service calendar
        return (self.calendar is None
                and self.calendar_dates is not None
                and len(self.calendar_dates) > 0)

    def _handle_alternate_calendar(self, date: str, valid_service_ids: Iterable) -> Iterable:
        """
        Returns the list of service ids active in the provided date, only if those ids
        are included in the provided valid service ids list.

        :param date:
        :param valid_service_ids:
        :return:
        """
        active_service_ids = []
        for s_id in valid_service_ids:
            ex_type = self._get_exception_for_service_date(service_id=s_id, date=date)

            if ex_type == 1:
                active_service_ids.append(s_id)

        return active_service_ids

    def _get_exception_for_service_date(self, service_id: Any, date: str) -> int:
        """
        Tries to retrieve the exception defined in the calendar_dates table for
        the provided date. Returns an integer code representing the exception type.

        :param date: date to check exception for
        :return: 3 different integer values:
            * -1 if no exception was found for the provided date
            * 1 if the service is exceptionally active in the provided date
            * 2 if the service is exceptionally not active in the provided date
        """

        try:
            # Extract exceptions for the provided service id
            service_exceptions: pd.DataFrame = self.calendar_dates[self.calendar_dates["service_id"] == service_id]

            # Extract the exception type for the provided date
            exception_on_date = service_exceptions[service_exceptions["date"] == date]
            exception_type = int(exception_on_date["exception_type"].iloc[0])

            return exception_type
        except IndexError:
            # Exception not found
            return -1


def read_gtfs_timetable(
        input_folder: str, departure_date: str, agency_names: List[str]
) -> GtfsTimetable:
    """Extract operators from GTFS data"""

    logger.info("Reading GTFS data")

    # Read agencies
    logger.debug("Reading Agencies")
    agencies_df = _process_agencies_table(input_folder=input_folder, agency_names=agency_names)

    agency_ids = agencies_df["agency_id"].values

    # Read routes
    logger.debug("Reading Routes")
    routes = _process_routes_table(input_folder=input_folder, agency_ids=agency_ids)

    # Read trips
    logger.debug("Reading Trips")
    trips = _process_trips_table(
        input_folder=input_folder,
        route_ids=routes["route_id"].values,
        dep_date=departure_date
    )

    # Read stop times
    logger.debug("Reading Stop Times")
    stop_times = _process_stop_times_table(input_folder=input_folder, trip_ids=trips["trip_id"].values)

    # Read stops (platforms)
    logger.debug("Reading Stops")
    stops = _process_stops_table(input_folder=input_folder, stop_times=stop_times)

    # Make sure stop times refer to the same stops as the processed
    stop_times = stop_times[stop_times["stop_id"].isin(stops["stop_id"])]

    logger.debug("Reading Transfers")
    transfers = _process_transfers_table(input_folder=input_folder, stop_ids=stops["stop_id"].values)

    gtfs_timetable = GtfsTimetable(
        original_gtfs_dir=input_folder,
        date=departure_date,
        trips=trips,
        stop_times=stop_times,
        stops=stops,
        routes=routes,
        transfers=transfers
    )

    return gtfs_timetable


def _process_agencies_table(input_folder: str, agency_names: Sequence[str]) -> pd.DataFrame:
    agencies_df = pd.read_csv(os.path.join(input_folder, "agency.txt"))

    # Filter only if at least one name is specified
    if len(agency_names) > 0:
        agencies_df = agencies_df.loc[agencies_df["agency_name"].isin(agency_names)][
            ["agency_id", "agency_name"]
        ]

    return agencies_df


def _process_routes_table(input_folder: str, agency_ids: Iterable) -> pd.DataFrame:
    routes = pd.read_csv(os.path.join(input_folder, "routes.txt"))

    routes = routes[routes.agency_id.isin(agency_ids)]
    routes = routes[
        ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"]
    ]

    return routes


def _process_trips_table(input_folder: str, route_ids: Iterable, dep_date: str) -> pd.DataFrame:
    trips = pd.read_csv(os.path.join(input_folder, "trips.txt"))
    trips = trips[trips.route_id.isin(route_ids)]

    trips_col_selector = [
        "route_id",
        "service_id",
        "trip_id"
    ]

    # The trip short name is an optionally defined attribute in the GTFS standard
    t_short_name_col = "trip_short_name"
    if t_short_name_col in trips.columns:
        trips_col_selector.append(t_short_name_col)

        trips[t_short_name_col] = trips[t_short_name_col].fillna(value="missing_short_name")
        trips[t_short_name_col] = trips[t_short_name_col].astype(str)

    trips = trips[trips_col_selector]

    # Read calendar
    logger.debug("Reading Calendar")
    calendar_processor = CalendarHandler(input_folder=input_folder)

    # Trips here are already filtered by agency ids
    valid_ids = trips["service_id"].values
    active_service_ids = calendar_processor.get_active_service_ids(
        on_date=dep_date,
        valid_service_ids=valid_ids
    )

    # Filter trips based on the service ids active on the provided dep. date
    trips = trips[trips["service_id"].isin(active_service_ids)]

    return trips


def _process_stop_times_table(input_folder: str, trip_ids: Iterable) -> pd.DataFrame:
    stop_times = pd.read_csv(
        os.path.join(input_folder, "stop_times.txt"), dtype={"stop_id": str}
    )
    stop_times = stop_times[stop_times.trip_id.isin(trip_ids)]
    col_selector = [
        "trip_id",
        "stop_sequence",
        "stop_id",
        "arrival_time",
        "departure_time"
    ]
    # Convert times to seconds
    stop_times["arrival_time"] = stop_times["arrival_time"].apply(str2sec)
    stop_times["departure_time"] = stop_times["departure_time"].apply(str2sec)

    # Contains distance data about the stop in its associated trip
    if "shape_dist_traveled" in stop_times.columns:
        col_selector.append("shape_dist_traveled")

    return stop_times[col_selector]


def _process_stops_table(input_folder: str, stop_times: pd.DataFrame) -> pd.DataFrame:
    stops_full = pd.read_csv(
        os.path.join(input_folder, "stops.txt"), dtype={"stop_id": str}
    )
    stops = stops_full.loc[
        stops_full["stop_id"].isin(stop_times.stop_id.unique())
    ].copy()

    # Add columns to the selector only if they exist in the stops dataframe
    stops_col_selector = [
        "stop_id",
        "stop_lat",  # added for Stop.geo field
        "stop_lon"  # added for Stop.geo field
    ]

    platform_code_col = "platform_code",
    if platform_code_col in stops.columns:
        stops_col_selector.append(platform_code_col)

    stop_name_col = "stop_name"
    if stop_name_col in stops.columns:
        stops_col_selector.append(stop_name_col)

    # Read stop areas, i.e. stations
    parent_station_col = "parent_station"
    if parent_station_col in stops.columns:
        stop_areas = stops[parent_station_col].unique()

        stops_col_selector.append(parent_station_col)
    else:
        stop_areas = []

    stops = pd.concat([stops, stops_full.loc[stops_full["stop_id"].isin(stop_areas)]])

    # Make sure that stop_code is of string type
    stop_code_col = "stop_code"
    stops[stop_code_col] = stops[stop_code_col].astype(str)

    stops[stop_code_col] = stops.stop_code.str.upper()

    # Filter out the general station rows (location_type == 1 and parent_station == empty)
    # Rationale is that general station are just "container stops" for their child stops
    if parent_station_col in stops.columns and "location_type" in stops.columns:
        to_remove_mask = stops[parent_station_col].isna()
        to_remove_mask &= (stops["location_type"].astype(str) == "1")
    else:
        to_remove_mask = np.zeros(shape=len(stops), dtype=bool)

    # Remove the general station rows and make sure that stops and stop_times
    # are all referring to the same stop_ids
    stops = stops.loc[~to_remove_mask]

    return stops[stops_col_selector]


def _process_transfers_table(input_folder: str, stop_ids: Iterable) -> pd.DataFrame:
    # Get transfers table only if it exists
    transfers_path = os.path.join(input_folder, "transfers.txt")

    if os.path.exists(transfers_path):
        logger.debug("Transfers Table exists")
        transfers = pd.read_csv(transfers_path)

        # Keep only the stops for the current date
        transfers = transfers[transfers["from_stop_id"].isin(stop_ids)
                              & transfers["to_stop_id"].isin(stop_ids)]
    else:
        transfers = None

    return transfers


def gtfs_to_pyraptor_timetable(
        gtfs_timetable: GtfsTimetable,
        n_jobs: int) -> RaptorTimetable:
    """
    Converts timetable to data structures suitable for the RAPTOR algorithm.

    :param gtfs_timetable: timetable instance
    :param n_jobs: number of parallel jobs to run
    :return: RAPTOR timetable
    """

    logger.info("Convert GTFS timetable to timetable for PyRaptor algorithm")

    # Stations and stops, i.e. platforms
    logger.debug("Adding stations and stops")
    stations, stops = _get_stations_and_stops(gtfs_timetable=gtfs_timetable)

    # Trips and Trip Stop Times
    logger.debug("Adding trips and trip stop times")
    trips, trip_stop_times = _get_trips_and_stop_times(
        gtfs_timetable=gtfs_timetable,
        stops=stops,
        n_jobs=n_jobs
    )

    # Routes
    logger.debug("Adding routes")
    routes = _get_routes(trips=trips)

    # Transfers
    logger.debug("Adding transfers")
    transfers = _get_transfers(gtfs_timetable=gtfs_timetable, stops=stops)

    # Timetable
    timetable = RaptorTimetable(
        stations=stations,
        stops=stops,
        trips=trips,
        trip_stop_times=trip_stop_times,
        routes=routes,
        transfers=transfers,
        original_gtfs_dir=gtfs_timetable.original_gtfs_dir,
        date=gtfs_timetable.date
    )

    return timetable


def _get_stations_and_stops(gtfs_timetable: GtfsTimetable) -> Tuple[Stations, Stops]:
    stations = Stations()
    stops = Stops()

    for s in gtfs_timetable.stops.itertuples():
        station = Station(s.stop_name, s.stop_name)
        station = stations.add(station)
        #   if station_id (same of first stop_name) is already present
        #   existing station with that station_id is returned

        platform_code = getattr(s, "platform_code", -1)
        stop_id = f"{s.stop_name}"
        stop = Stop(s.stop_id, stop_id, station, platform_code, stops.last_index + 1,
                    Coordinates(s.stop_lat, s.stop_lon))

        station.add_stop(stop)
        stops.add_stop(stop)

    return stations, stops


def _get_trips_and_stop_times(
        gtfs_timetable: GtfsTimetable,
        stops: Stops,
        n_jobs: int
) -> Tuple[Trips, TripStopTimes]:
    logger.debug("Extracting transport type for each trip")

    trip_route_info: dict[Any, RouteInfo] = {}
    trips_and_routes: pd.DataFrame = pd.merge(gtfs_timetable.trips, gtfs_timetable.routes, on="route_id")
    for row in trips_and_routes.itertuples():
        route_name = getattr(row, "route_long_name", None)
        route_name = route_name if route_name is not None else getattr(row, "route_short_name", "missing_route_name")
        trip_route_info[row.trip_id] = RouteInfo(name=route_name, transport_type=TransportType(int(row.route_type)))

    # Stop Times
    stop_times = defaultdict(list)
    for stop_time in gtfs_timetable.stop_times.itertuples():
        stop_times[stop_time.trip_id].append(stop_time)

    jobs = []
    for i in range(n_jobs):
        total_trips = len(gtfs_timetable.trips)
        interval_length = math.floor(total_trips / n_jobs)
        start = i * interval_length

        if i == (n_jobs - 1):
            # Make sure that all the trips are processed and
            # that no trip is left out due to rounding errors
            # in calculating interval_length
            end = total_trips
        else:
            end = (start + interval_length) - 1  # -1 because the interval_length-th trip belongs to the next round

        job = _trips_processor_job(
            # +1 because end would not be included
            trips_row_iterator=itertools.islice(gtfs_timetable.trips.itertuples(), start, end+1),
            stops_info=stops,
            trip_route_info=trip_route_info,
            stop_times_by_trip_id=stop_times,
            job_id=f"#{i}"
        )
        jobs.append(job)

    logger.debug(f"Starting {n_jobs} jobs to process timetable trips")
    job_results = _execute_jobs(jobs=jobs, cpus=n_jobs)

    trips = Trips()
    trip_stop_times = TripStopTimes()
    for res_trips, res_stop_times in job_results:
        # Add the results
        for res_trip in res_trips.set_idx.values():
            trips.add(res_trip)

        for res_trip_stop_time in res_stop_times.set_idx.values():
            trip_stop_times.add(res_trip_stop_time)

    return trips, trip_stop_times


def _get_routes(trips: Trips) -> Routes:
    routes = Routes()
    for trip in trips:
        trip_route = routes.add(trip)

        # Update each trip with the actual route instance it refers to
        trip.route_info = RouteInfo(
            name=trip.route_info.name,
            transport_type=trip.route_info.transport_type,
            route=trip_route
        )

    return routes


def _get_transfers(gtfs_timetable: GtfsTimetable, stops: Stops) -> Transfers:
    transfers = Transfers()

    # Add transfers based on the transfers.txt table, if it exists
    if gtfs_timetable.transfers is not None:
        for t_row in gtfs_timetable.transfers.itertuples():
            from_stop = stops[t_row.from_stop_id]
            to_stop = stops[t_row.to_stop_id]
            t_time = t_row.min_transfer_time

            t = Transfer(from_stop=from_stop, to_stop=to_stop, transfer_time=t_time)
            transfers.add(t)

    return transfers


def _trips_processor_job(
        trips_row_iterator: Iterable[NamedTuple],
        stops_info: Stops,
        trip_route_info: Mapping[Any, RouteInfo],
        stop_times_by_trip_id: Mapping[Any, List],
        job_id: uuid.UUID | int | str = uuid.uuid4()
) -> Callable[[], Tuple[Trips, TripStopTimes]]:
    """
    Returns a function that processes the provided trips.

    :param trips_row_iterator: iterator that cycles over the rows of
        a GtfsTimetable.trips dataframe
    :param stops_info: collection of stop instances that contain detailed information
        for each stop
    :param trip_route_info: mapping that pairs trip ids with an object containing
        information about the route that each trip belongs to
    :param stop_times_by_trip_id: default dictionary where keys are trip ids and
        values are collections of stop times.
    :param job_id: id to assign to this job.
        Its purpose is only to identify the job in the logger output.
    :return: tuple containing the generated collections of Trip and TripStopTime instances
    """

    def job():
        def log(msg: str):
            logger.debug(f"[TripsProcessor {job_id}] {msg}")

        trips = Trips()
        trip_stop_times = TripStopTimes()

        # DEBUG: Keep track of progress since this operation is relatively heavy
        processed_trips = -1
        prev_pct_point = -1

        trip_rows = list(trips_row_iterator)
        for row in trip_rows:
            processed_trips += 1
            table_length = len(trip_rows)
            current_pct = math.floor((processed_trips / table_length) * 100)

            if math.floor(current_pct) > prev_pct_point or current_pct == 100:
                log(f"Progress: {current_pct}% [trip #{processed_trips} of {table_length}]")
                prev_pct_point = current_pct

            # Transport description as hint
            trip_id = row.trip_id
            route_info = trip_route_info[trip_id]
            trip = Trip(id_=trip_id, route_info=route_info)

            # Iterate over stops, ordered by sequence number:
            # the first stop will be the one with stop_sequence == 1
            sort_stop_times = sorted(
                stop_times_by_trip_id[row.trip_id], key=lambda s: int(s.stop_sequence)
            )
            for stop_number, stop_time in enumerate(sort_stop_times):
                # Timestamps
                dts_arr = stop_time.arrival_time
                dts_dep = stop_time.departure_time
                trav_dist = stop_time.shape_dist_traveled

                # Trip Stop Times
                stop = stops_info.get_stop(stop_time.stop_id)

                trip_stop_time = TripStopTime(
                    trip=trip,
                    stop_idx=stop_number,
                    stop=stop,
                    dts_arr=dts_arr,
                    dts_dep=dts_dep,
                    travelled_distance=trav_dist
                )

                trip_stop_times.add(trip_stop_time)
                trip.add_stop_time(trip_stop_time)

            # Add trip
            if trip:
                trips.add(trip)

        log(f"Processing completed")

        return trips, trip_stop_times

    return job


def add_shared_mobility_to_pyraptor_timetable(timetable: RaptorTimetable, feeds: str, n_jobs: int) -> RaptorTimetableSM:
    """
    Adds shared mobility data to the provided timetable.

    :param timetable: timetable instance
    :param feeds: path to the file containing shared mobility feed info
    :param n_jobs: number of jobs to execute when processing shared mob data
    """

    logger.info("Adding shared mobility datas")

    feed_infos: List[Dict] = json.load(open(feeds))['feeds']
    feeds: List[SharedMobilityFeed] = [SharedMobilityFeed(feed_info['url'], feed_info['lang'])
                                       for feed_info in feed_infos]

    logger.debug("Adding stations and renting-stations")

    stations_before_sm, stops_before_sm = len(timetable.stations), len(timetable.stops)  # debugging

    for feed in feeds:
        for renting_station in feed.renting_stations:
            timetable.stops.add_stop(renting_station)
            timetable.stations.add(renting_station.station)

        stations_after_sm, stops_after_sm = len(timetable.stations), len(timetable.stops)  # debugging
        logger.debug(f"Added {stations_after_sm - stations_before_sm} new stations from {feed.system_id}")
        logger.debug(f"Added {stops_after_sm - stops_before_sm} new renting stations from {feed.system_id}")
        stations_before_sm, stops_before_sm = stations_after_sm, stops_after_sm

    logger.debug("Adding vehicle-transfers")

    # Number of transfers before shared mob - for debugging/logging purposes
    transfers_before_sm = len(timetable.transfers)

    public_stops = public_transport_stop(for_stops=timetable.stops)
    shared_mob_stops = shared_mobility_stops(for_stops=timetable.stops)

    jobs = []
    for i in range(n_jobs):
        total_sm_stops = len(shared_mob_stops)
        interval_length = math.floor(total_sm_stops / n_jobs)
        start = i * interval_length

        if i == (n_jobs - 1):
            # Make sure that all the shared mob stops are processed and
            # that no stop is left out due to rounding errors
            # in calculating interval_length
            end = total_sm_stops
        else:
            end = (start + interval_length) - 1  # -1 because the interval_length-th stop belongs to the next round

        job = _shared_mob_processor_job(
            # +1 because end would not be included
            shared_mob_stops=list(itertools.islice(shared_mob_stops, start, end+1)),
            public_stops=public_stops,
            job_id=f"#{i}"
        )
        jobs.append(job)

    logger.debug(f"Starting {n_jobs} jobs to process timetable trips")
    job_results = _execute_jobs(jobs=jobs, cpus=n_jobs)

    for transfer in itertools.chain.from_iterable(job_results):
        timetable.transfers.add(transfer)

    # Number of public transport + shared mobility transfers - for debugging/logging purposes
    transfers_after_sm = len(timetable.transfers)
    logger.debug(f"Added new {transfers_after_sm - transfers_before_sm} vehicle-transfers "
                 f"between public and shared-mobility stops")
    return RaptorTimetableSM(
        stations=timetable.stations,
        stops=timetable.stops,
        trips=timetable.trips,
        trip_stop_times=timetable.trip_stop_times,
        routes=timetable.routes,
        transfers=timetable.transfers,
        shared_mobility_feeds=feeds
    )


def _shared_mob_processor_job(
        shared_mob_stops: Sequence[Stop],
        public_stops: Sequence[Stop],
        job_id: int | str | uuid.UUID = uuid.uuid4()
) -> Callable[[], Iterable[Transfer]]:
    """
    Returns a function that creates and returns transfers between
    the provided shared mobility stops and public stops (i.e. GTFS stops)

    :param shared_mob_stops: stops related to shared mobility
    :param public_stops: stops related to public transit
    :param job_id: id to assign to this job.
        Its purpose is only to identify the job in the logger output.
    :return: collection of transfers between the aforementioned stops
    """

    def job():
        def log(msg: str):
            logger.debug(f"[SharedMobProcessor {job_id}] {msg}")

        transfers = []
        for i, mob in enumerate(shared_mob_stops):
            log(f'Progress: {i * 100 / len(shared_mob_stops):0.0f}% '
                f'[Stop #{i} of {len(shared_mob_stops)}]')

            for pub in public_stops:
                if mob.distance_from(pub) < MIN_DIST:
                    both_way_transfers = Transfer.get_transfer(mob, pub)
                    transfers.extend(both_way_transfers)

        return transfers

    return job


# TypeVar for job results
_J = TypeVar('_J')


def _execute_jobs(
        jobs: Iterable[Callable[[], _J]],
        cpus: int = cpu_count()
) -> Iterable[_J]:
    job_results: dict[int, ApplyResult] = {}  # if jobs == -1, auto-detect
    pool = p.ProcessPool(nodes=cpus)
    for i, job in enumerate(jobs):
        job_id = i
        logger.debug(f"Starting Job #{job_id}...")

        job_results[job_id] = pool.apipe(job)

    logger.debug(f"Waiting for jobs to finish...")
    for job_id, result in job_results.items():
        res: _J = result.get()
        logger.debug(f"Job #{job_id} has completed its execution")

        yield res


if __name__ == "__main__":
    args = parse_arguments()
    generate_timetable(input_folder=args.input, output_folder=args.output,
                       departure_date=args.date, agencies=args.agencies,
                       shared_mobility=args.shared_mobility, feeds_path=args.feeds,
                       n_jobs=args.jobs)
