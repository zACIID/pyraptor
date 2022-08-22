from loguru import logger
import json
import argparse

from pyraptor.dao.timetable import read_timetable
from pyraptor.model.timetable import RaptorTimetable


def parse_arguments():
    """Parse arguments"""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-i",
        "--input",
        type=str,
        default="data/output/milan",
        help="Input directory",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="data/output/milan/a_star",
        help="Output directory",
    )

    arguments = parser.parse_args()
    return arguments


def main(
        input_folder: str,
        output_folder: str
):
    """Run preprocess for A Star algorithm"""

    logger.debug("Input directory       : {}", input_folder)
    logger.debug("Output directory      : {}", output_folder)

    timetable = read_timetable(input_folder)

    get_adj_list(timetable, output_folder)


class Step(object):
    """
    Step object to represent the weight of an edge of the graph
    It contains information about the departure from a stop to another, the route this step is part of, and what transport is used for it
    """

    def __init__(self, stop, duration, departure_time, arrive_time, trip_id, route_id, transport_type):
        """
        Initializes the stop node
        :param stop: destination stop of the trip
        :param duration: duration of the trip
        :param departure_time: arrive time from a stop
        :param arrive_time: arrive time to a stop
        :param trip_id: trip id of this step
        :param route_id: route id of this step
        :param transport_type: transport type of this route (route_type : Indicates the type of transportation used on a route)
        """

        self.stop_to = stop
        self.duration = duration
        self.departure_time = departure_time
        self.arrive_time = arrive_time
        self.trip_id = trip_id
        self.route_id = route_id
        self.transport_type = transport_type

    def set_duration(self, duration):
        """set a new duration value"""
        self.duration = duration


# def manhattan_heuristic(stop1, stop2) -> float:
#     """
#     Manhattan distance between two stops
#     :param stop1: Stop
#     :param stop2: Stop
#     :return: float distance
#     """
#     (x1, y1) = (stop1.lat, stop1.long)
#     (x2, y2) = (stop2.lat, stop2.long)
#     return abs(x1 - x2) + abs(y1 - y2)
#
#
# def euclidean_heuristic(stop1, stop2) -> float:
#     """
#     Euclidean distance between two stops
#     :param stop1: Stop
#     :param stop2: Stop
#     :return: float distance
#     """
#     return np.linalg.norm(stop1 - stop2)


def get_heuristic(destination, timetable: RaptorTimetable) -> dict[str, float]:
    """
    Time = distance/speed [hour]
    we use the average public transport speed in Italy [km/h]
    reference:
    https://www.statista.com/statistics/828636/public-transport-trip-speed-by-location-in-italy/
    we won't use Manhattan distance or Euclidean distance since we want the fastest travel not the shortest

    :param destination: destination station
    :param timetable: timetable
    :return: assign an heuristic value to every stops
    """
    heuristic = {}
    avg_speed = (14 + ((47 + 56) / 2)) / 2

    for st in timetable.stops:
        heuristic[st.id] = (st.distance_from(destination) / avg_speed)*3600

    return heuristic


def get_adj_list(timetable: RaptorTimetable, output_folder):
    """
    contains all the neighbouring stops for some stop

    :param timetable: timetable
    :param output_folder: output directory
    :return: create adjacency list
    """

    adjacency_list = {}

    # per ogni fermata
    #   trovo tutte le fermate che riesce a raggiungere tramite un trip
    #   mi salvo la fermata vicina + duration, arrive_time, route_id, transport_type
    #   in qualche modo me li ricavo, transport type sta in route.txt, arrive_time e route_id ci sono gia stop_times.txt
    #   duration me la calcolo, come?
    #   duration deve essere calcolato anche considerando possibili momenti di attesa alla fermata per prendere il mezzo
    for st in timetable.stops:

        is_present_in_trip = {}
        # get all trips where st is in
        for arr in timetable.trips:
            if st.id in arr.trip_stop_ids():
                is_present_in_trip[arr.id] = arr

        # get all the next stops in the same trips of st
        for tripid, tr in is_present_in_trip.items():
            adjacency_list[st.id] = []
            got_seq = False
            seq = -2
            dep = 0
            for s in tr.stop_times:
                if s.stop.id == st.id and not got_seq:
                    seq = s.stop_idx
                    got_seq = True
                    dep = s.dts_arr
                if got_seq and s.stop_idx == seq+1:
                    adjacency_list[st.id].append(Step(s.stop,
                                                      s.dts_arr-dep,
                                                      dep,
                                                      s.dts_arr,
                                                      tripid,
                                                      tr.route_info.name,
                                                      tr.route_info.transport_type.name))

        for arr in timetable.transfers:
            if arr.from_stop == st:
                adjacency_list[st.id].append(Step(arr.to_stop, arr.transfer_time, "x", "x", arr.id, "x", "walk"))
                # departure time, arrive time and route id set to "x" because it's a transfer

    write_adjacency(output_folder, adjacency_list)


def write_heuristic(output_folder: str, heuristic: dict[str, float]) -> None:
    """
    Write the heuristic list to output directory
    """
    with open(output_folder + '/heuristic.json', 'w') as outfile:
        json.dump(heuristic, outfile, indent=4)

    logger.debug("heuristic ok")


def read_heuristic(input_folder: str) -> dict[str, float]:
    """
    Read the heuristic data from the cache directory
    """
    with open(input_folder + '/heuristic.json', 'r') as file:
        data = json.load(file)

    logger.debug("heuristic ok")

    return data


def write_adjacency(output_folder: str, adjacency_list) -> None:
    """
    Write the adjacency list to output directory
    """
    with open(output_folder+'/adjacency.json', 'w') as outfile:
        json.dump(adjacency_list, outfile, indent=4)

    logger.debug("adjacency ok")


def read_adjacency(input_folder: str):
    """
    Read the adjacency list data from the cache directory
    """
    with open(input_folder+'/adjacency.json', 'r') as file:
        data = json.load(file)

    logger.debug("adjacency ok")

    return data


if __name__ == "__main__":
    args = parse_arguments()
    main(
        input_folder=args.input,
        output_folder=args.output
    )

# todo non va bene json
# TypeError: Object of type Step is not JSON serializable
