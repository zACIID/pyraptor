"""Test Query Raptor"""
from pyraptor import query_raptor
from pyraptor.model.timetable import RaptorTimetable


def test_has_main():
    """Has main"""
    assert query_raptor.generate_timetable


def test_query_raptor(default_timetable: RaptorTimetable):
    """Test query raptor"""
    origin_station = "A"
    destination_station = "F"
    dep_secs = 0
    rounds = 4

    journey_to_destinations = query_raptor.run_raptor_config(
        default_timetable,
        origin_station,
        dep_secs,
        rounds,
    )
    journey = journey_to_destinations[destination_station]
    assert journey is not None, "destination should be reachable"

    journey.print(dep_secs=dep_secs)

    assert len(journey) == 3, "should have 3 trips in journey"
