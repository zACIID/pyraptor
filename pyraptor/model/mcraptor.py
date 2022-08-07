"""McRAPTOR algorithm"""
from typing import List, Tuple, Dict
from copy import copy
from time import perf_counter

from loguru import logger
from pyraptor.model.structures import (
    Timetable,
    Stop,
    Route,
    Bag,
    MultiCriteriaLabel,
    Leg,
    Journey,
    pareto_set,
    TransferTrip,
    TransportType,
)


class McRaptorAlgorithm:
    """McRAPTOR Algorithm"""

    def __init__(self, timetable: Timetable):
        self.timetable = timetable

    def run(
            self, from_stops: List[Stop], dep_secs: int, rounds: int, previous_run: Dict[Stop, Bag] = None
    ) -> Tuple[Dict[int, Dict[Stop, Bag]], int]:
        """Run Round-Based Algorithm"""

        s = perf_counter()

        # Initialize empty bag, i.e. B_k(p) = [] for every k and p
        bag_round_stop: Dict[int, Dict[Stop, Bag]] = {}
        for k in range(0, rounds + 1):
            bag_round_stop[k] = {}
            for p in self.timetable.stops:
                bag_round_stop[k][p] = Bag()

        logger.debug(f"Starting from Stop IDs: {str(from_stops)}")

        # Initialize bag for round 0, i.e. add Labels with criterion 0 for all from stops
        if previous_run is not None:
            # For the range query
            bag_round_stop[0] = copy(previous_run)

        # Add origin stops to bag
        for from_stop in from_stops:
            bag_round_stop[0][from_stop].add(
                MultiCriteriaLabel(
                    earliest_arrival_time=dep_secs,
                    fare=0.0,
                    trip=None,
                    boarding_stop=from_stop
                )
            )

        marked_stops = from_stops

        # Run rounds
        actual_rounds = 0
        for k in range(1, rounds + 1):
            logger.info(f"Analyzing possibilities round {k}")
            logger.debug(f"Stops to evaluate count: {len(marked_stops)}")

            # Copy bag from previous round
            bag_round_stop[k] = copy(bag_round_stop[k - 1])

            if len(marked_stops) > 0:
                actual_rounds = k

                # Accumulate routes serving marked stops from previous round
                # i.e. get the best (r,p) pairs where p is the first stop reachable in route r
                route_marked_stops = self.accumulate_routes(marked_stops)

                # Traverse each route
                # i.e. update the labels of each marked stop, assigning to each stop
                #   the earliest trip that can be caught and the earliest arrival
                #   time of that trip at that stop
                bag_round_stop, marked_stops_trips = self.traverse_route(
                    bag_round_stop, k, route_marked_stops
                )

                # Now add (footpath) transfers between stops and update
                # i.e. update the arrival time to each stop by considering (foot) transfers
                bag_round_stop, marked_stops_transfers = self.add_transfer_time(
                    bag_round_stop, k, marked_stops_trips
                )

                marked_stops = set(marked_stops_trips).union(marked_stops_transfers)
            else:
                break

        logger.info("Finish round-based algorithm to create bag with best labels")
        logger.info(f"Running time: {perf_counter() - s}")

        return bag_round_stop, actual_rounds

    def accumulate_routes(self, marked_stops) -> List[Tuple[Route, Stop]]:
        """Accumulate routes serving marked stops from previous round, i.e. Q"""

        route_marked_stops = {}  # i.e. Q
        for marked_stop in marked_stops:
            routes_serving_stop = self.timetable.routes.get_routes_of_stop(marked_stop)

            for route in routes_serving_stop:
                # Check if new_stop is before existing stop in Q
                current_stop_for_route = route_marked_stops.get(route, None)  # p'
                if (current_stop_for_route is None) or (
                        route.stop_index(current_stop_for_route) > route.stop_index(marked_stop)
                ):
                    route_marked_stops[route] = marked_stop
        route_marked_stops = [(r, p) for r, p in route_marked_stops.items()]

        logger.debug(f"Found {len(route_marked_stops)} routes serving marked stops")

        return route_marked_stops

    def traverse_route(
            self,
            bag_round_stop: Dict[int, Dict[Stop, Bag]],
            k: int,
            route_marked_stops: List[Tuple[Route, Stop]],
    ) -> Tuple[Dict[int, Dict[Stop, Bag]], List[Stop]]:
        """
        Traverse through all marked route-stops and update labels accordingly.

        :param bag_round_stop: Bag per round per stop
        :param k: current round
        :param route_marked_stops: list of marked (route, stop) for evaluation
        """

        new_marked_stops = set()

        for marked_route, marked_stop in route_marked_stops:
            # Traversing through route from marked stop
            route_bag = Bag()

            # Get all stops after current stop within the current route
            marked_stop_index = marked_route.stop_index(marked_stop)
            remaining_stops_in_route = marked_route.stops[marked_stop_index:]

            # The following steps refer to the three-part processing done for each stop,
            # described in the McRAPTOR section of the MSFT paper.
            for current_stop_idx, current_stop in enumerate(remaining_stops_in_route):

                # Step 1: update the earliest arrival times and criteria for each label L in route-bag
                updated_labels = []
                for label in route_bag.labels:
                    # Get the arrival time of the trip at the current stop
                    trip_stop_time = label.trip.get_stop(current_stop)

                    # Take fare of previous stop in trip as fare is defined on start
                    # TODO this is another part relevant for the multi-criteria approach:
                    #   here fares for each label are updated based on the traversed trips

                    # TODO need to understand how fare is actually updated: what is tripstoptime.fare?
                    #   the cumulative fare of the trip up until that point? in similar fashion,
                    #   co2 and distance attributes can be defined and initialized with
                    #   stop_times.shape_dist data
                    previous_stop = remaining_stops_in_route[current_stop_idx - 1]
                    from_fare = label.trip.get_fare(previous_stop)

                    # TODO since criteria should not be hardcoded, but dependent on an external
                    #   config file, find a way to abstract the update method from the actual criteria
                    label = label.update(
                        earliest_arrival_time=trip_stop_time.dts_arr,
                        fare_addition=from_fare,
                    )

                    updated_labels.append(label)

                # Route bag B_{r} basically represents all the updated labels of
                #   the stops in the current marked route. Notably, each updated label means
                #   that you can board a trip with better characteristics
                #   (i.e. earlier arrival time, better fares, lesser number of trips)
                route_bag = Bag(labels=updated_labels)

                # Step 2: merge bag_route into bag_round_stop and remove dominated labels
                # NOTE: merging the current stop bag with route bag basically means to keep
                #   the labels that allow to get to the current stop in the most efficient way
                bag_round_stop[k][current_stop] = bag_round_stop[k][current_stop].merge(
                    route_bag
                )
                bag_update = bag_round_stop[k][current_stop].updated

                # Mark stop if bag is updated.
                # Updated bag means that the current stop brought some improvements
                if bag_update:
                    new_marked_stops.add(current_stop)

                # Step 3: merge B_{k-1}(p) into B_r
                # Merging here creates a bag where the best labels from the previous round,
                #   for the current stop, and the best from the current route are kept
                route_bag = route_bag.merge(bag_round_stop[k - 1][current_stop])

                # Assign trips to all newly added labels in route_bag
                # This is the trip on which we board
                # This step is needed because the labels from the previous round need
                #   to be updated with the new earliest boardable trip
                updated_labels = []
                for label in route_bag.labels:
                    earliest_trip = marked_route.earliest_trip(
                        label.earliest_arrival_time, current_stop
                    )
                    if earliest_trip is not None:
                        # Update label with the earliest trip in route leaving from this station
                        # If trip is different, we board the trip at current_stop
                        label = label.update_trip(earliest_trip, current_stop)
                        updated_labels.append(label)
                route_bag = Bag(labels=updated_labels)

        logger.debug(f"{len(new_marked_stops)} reachable stops added")

        return bag_round_stop, list(new_marked_stops)

    def add_transfer_time(
            self,
            bag_round_stop: Dict[int, Dict[Stop, Bag]],
            k: int,
            marked_stops: List[Stop],
    ) -> Tuple[Dict[int, Dict[Stop, Bag]], List[Stop]]:
        """
        Updates each stop (label) earliest arrival time by also considering (footpath) transfers between stops.

        :param bag_round_stop: dictionary of stop bags keyed by round
        :param k: current round
        :param marked_stops: list of the currently marked stops
        :return:
        """

        marked_stops_transfers = set()

        # Add in transfers to other platforms (same station) and stops
        for current_stop in marked_stops:
            # Note: transfers are transitive, which means that for each reachable stops (a, b) there
            # is transfer (a, b) as well as (b, a)
            other_station_stops = [t.to_stop for t in self.timetable.transfers if t.from_stop == current_stop]

            for other_stop in other_station_stops:
                # Create temp copy of B_k(p_i)
                temp_bag = Bag()
                for label in bag_round_stop[k][current_stop].labels:
                    # Add arrival time to each label
                    transfer_arrival_time = (
                            label.earliest_arrival_time
                            + self.get_transfer_time(current_stop, other_stop)
                    )
                    # Update label with new earliest arrival time at other_stop
                    # NOTE: Each label contains the trip with which one arrives at the current stop
                    #   with k legs by boarding the trip at from_stop, along with the criteria
                    #   (i.e. boarding time, fares, number of legs)
                    label = label.update(
                        earliest_arrival_time=transfer_arrival_time,
                        fare_addition=0,
                        boarding_stop=current_stop,
                    )

                    # Update the trip with which to arrive at other_stop with a transfer trip
                    transfer_trip = TransferTrip(
                        from_stop=current_stop,
                        to_stop=other_stop,
                        dep_time=label.earliest_arrival_time,
                        arr_time=transfer_arrival_time,

                        # TODO add method or field `transfer_type` to Transfer class
                        #  such accesser is then overrode by shared mobility Transfer sub-classes
                        transport_type=TransportType.Walk
                    )
                    label = label.update_trip(
                        trip=transfer_trip,
                        boarding_stop=current_stop
                    )
                    temp_bag.add(label)

                # Merge temp bag into B_k(p_j)
                bag_round_stop[k][other_stop] = bag_round_stop[k][other_stop].merge(
                    temp_bag
                )
                bag_update = bag_round_stop[k][other_stop].updated

                # Mark stop if bag is updated
                if bag_update:
                    marked_stops_transfers.add(other_stop)

        logger.debug(f"{len(marked_stops_transfers)} transferable stops added")

        return bag_round_stop, list(marked_stops_transfers)

    def get_transfer_time(self, stop_from: Stop, stop_to: Stop) -> int:
        """
        Calculate the transfer time from a stop to another stop (usually at one station)
        """
        transfers = self.timetable.transfers
        return transfers.stop_to_stop_idx[(stop_from, stop_to)].transfer_time


def best_legs_to_destination_station(
        to_stops: List[Stop], last_round_bag: Dict[Stop, Bag]
) -> List[Leg]:
    """
    Find the last legs to destination station that are reached by non-dominated labels.
    """

    # Find all labels to target_stops
    best_labels = [
        (stop, label) for stop in to_stops for label in last_round_bag[stop].labels
    ]

    # TODO Use merge function on Bag
    # Pareto optimal labels
    pareto_optimal_labels = pareto_set([label for (_, label) in best_labels])
    pareto_optimal_labels = [
        (stop, label) for (stop, label) in best_labels if label in pareto_optimal_labels
    ]

    # Label to leg, i.e. add to_stop
    legs = [
        Leg(
            label.boarding_stop,
            to_stop,
            label.trip,
            label.earliest_arrival_time,
            label.fare,
            label.n_trips,
        )
        for (to_stop, label) in pareto_optimal_labels
    ]
    return legs


def reconstruct_journeys(
        from_stops: List[Stop],
        destination_legs: List[Leg],
        bag_round_stop: Dict[int, Dict[Stop, Bag]],
        k: int,
) -> List[Journey]:
    """
    Construct Journeys for destinations from bags by recursively
    looping from destination to origin.
    """

    # TODO debug
    if "qt8 m1--1" in [leg.to_stop.name for leg in destination_legs]:
        print("here")

    def loop(
            bag_round_stop: Dict[int, Dict[Stop, Bag]], k: int, journeys: List[Journey]
    ):
        """Create full journey by prepending legs recursively"""

        last_round_bags = bag_round_stop[k]

        for jrny in journeys:
            current_leg = jrny[0]

            # End of journey if we are at origin stop or journey is not feasible
            if current_leg.trip is None or current_leg.from_stop in from_stops:
                jrny = jrny.remove_empty_and_same_station_legs()

                # Journey is valid if leg k ends before the start of leg k+1
                if jrny.is_valid() is True:
                    yield jrny
                continue

            # Loop trough each new leg. These are the legs that come before the current and that lead to from_stop
            labels_to_from_stop = last_round_bags[current_leg.from_stop].labels
            for new_label in labels_to_from_stop:
                new_leg = Leg(
                    new_label.boarding_stop,
                    current_leg.from_stop,
                    new_label.trip,
                    new_label.earliest_arrival_time,
                    new_label.fare,
                    new_label.n_trips,
                )
                # Only prepend new_leg if compatible before current leg, e.g. earlier arrival time, etc.
                if new_leg.is_compatible_before(current_leg):
                    new_jrny = jrny.prepend_leg(new_leg)
                    for i in loop(bag_round_stop, k, [new_jrny]):
                        yield i

    journeys = [Journey(legs=[leg]) for leg in destination_legs]
    journeys = [jrny for jrny in loop(bag_round_stop, k, journeys)]

    return journeys
