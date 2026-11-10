#!/usr/bin/env python3
"""Two boxes behind one endpoint, the same cases as tests/test_proxy_pair.py with the entry in this process: what
CI's coverage floor measures.

The pair's tests are four files since 2026-11-10, so that CI runs them side by side: this one,
tests/test_proxy_pair.py, tests/test_proxy_pair_one_model.py and
tests/test_proxy_pair_one_model_inprocess.py. What they share (the two
boxes, their identities, the fake engines, PairFixture) is tests/pairbox.py.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# the module and not its class: unittest runs every TestCase a module names, so the class named here would run
# its cases a second time in this file
import test_proxy_pair as two_models  # noqa: E402


# The same cases with the entry in this process: what CI's coverage floor measures. Each is declared again
# rather than only inherited, as in test_proxy_hold.py: CI counts the tests a file declares against the tests
# that ran, and an inherited case runs without being declared.
class TwoBoxesBehindOneEndpointInProcess(two_models.TwoBoxesBehindOneEndpoint):
    INPROC = True

    def test_a_box_serving_its_own_model_writes_no_line_on_the_other(self):
        super().test_a_box_serving_its_own_model_writes_no_line_on_the_other()

    def test_a_lane_this_box_serves_stays_here(self):
        super().test_a_lane_this_box_serves_stays_here()

    def test_a_lane_the_peer_serves_goes_there_with_the_peers_key(self):
        super().test_a_lane_the_peer_serves_goes_there_with_the_peers_key()

    def test_the_ollama_and_sagemaker_routes_go_to_the_box_that_serves_too(self):
        super().test_the_ollama_and_sagemaker_routes_go_to_the_box_that_serves_too()

    def test_a_stream_from_the_peer_arrives_whole_and_in_order(self):
        super().test_a_stream_from_the_peer_arrives_whole_and_in_order()

    def test_the_anthropic_dialect_goes_to_the_peer_too(self):
        super().test_the_anthropic_dialect_goes_to_the_peer_too()

    def test_any_other_name_stays_on_this_box_as_on_a_box_alone(self):
        super().test_any_other_name_stays_on_this_box_as_on_a_box_alone()

    def test_either_box_is_an_entry(self):
        super().test_either_box_is_an_entry()

    def test_a_lane_no_box_serves_is_answered_here_as_on_a_box_alone(self):
        super().test_a_lane_no_box_serves_is_answered_here_as_on_a_box_alone()

    def test_the_anthropic_dialect_of_a_lane_no_box_serves_is_answered_here_too(self):
        super().test_the_anthropic_dialect_of_a_lane_no_box_serves_is_answered_here_too()

    def test_a_peer_that_switched_since_its_list_was_read_never_serves_the_wrong_model(self):
        super().test_a_peer_that_switched_since_its_list_was_read_never_serves_the_wrong_model()

    def test_a_hop_header_on_the_client_port_is_no_hop(self):
        super().test_a_hop_header_on_the_client_port_is_no_hop()

    def test_a_peer_that_does_not_answer_is_said_at_once(self):
        super().test_a_peer_that_does_not_answer_is_said_at_once()

    def test_the_models_list_names_both_boxes(self):
        super().test_the_models_list_names_both_boxes()

    def test_the_models_list_never_waits_for_a_peer(self):
        super().test_the_models_list_never_waits_for_a_peer()

    def test_no_secret_reaches_a_journal(self):
        super().test_no_secret_reaches_a_journal()

    def test_no_key_or_cookie_crosses_the_pair(self):
        super().test_no_key_or_cookie_crosses_the_pair()

    def test_the_pair_port_takes_the_pairs_routes_only(self):
        super().test_the_pair_port_takes_the_pairs_routes_only()

    def test_a_box_without_pair_json_is_what_it_was(self):
        super().test_a_box_without_pair_json_is_what_it_was()

    def test_a_pair_json_without_a_peer_is_a_box_alone(self):
        super().test_a_pair_json_without_a_peer_is_a_box_alone()

    def test_a_pair_json_that_does_not_parse_leaves_the_box_alone(self):
        super().test_a_pair_json_that_does_not_parse_leaves_the_box_alone()

    def test_a_request_on_the_pair_port_is_a_hop_from_the_box_of_its_certificate(self):
        super().test_a_request_on_the_pair_port_is_a_hop_from_the_box_of_its_certificate()

    def test_the_other_boxs_rows_and_summary_are_rebuilt_from_their_fields(self):
        super().test_the_other_boxs_rows_and_summary_are_rebuilt_from_their_fields()

    def test_an_ndjson_stream_from_the_other_box_arrives_as_it_comes(self):
        super().test_an_ndjson_stream_from_the_other_box_arrives_as_it_comes()

    def test_the_other_boxs_end_by_order_line_never_reaches_an_ollama_client(self):
        super().test_the_other_boxs_end_by_order_line_never_reaches_an_ollama_client()

    def test_an_ndjson_stream_from_the_other_box_waits_out_a_long_prefill(self):
        super().test_an_ndjson_stream_from_the_other_box_waits_out_a_long_prefill()

    def test_the_other_box_never_names_the_engines_requests_here(self):
        super().test_the_other_box_never_names_the_engines_requests_here()

    def test_the_cockpits_stop_is_never_taken_from_the_pair_port(self):
        super().test_the_cockpits_stop_is_never_taken_from_the_pair_port()

    def test_the_pair_port_refuses_a_box_without_a_pinned_certificate(self):
        super().test_the_pair_port_refuses_a_box_without_a_pinned_certificate()

    def test_a_certificate_that_chains_to_a_pinned_one_is_not_it(self):
        super().test_a_certificate_that_chains_to_a_pinned_one_is_not_it()

    def test_an_entry_never_sends_to_a_box_without_the_pinned_certificate(self):
        super().test_an_entry_never_sends_to_a_box_without_the_pinned_certificate()

    def test_a_box_taken_out_of_the_pair_is_refused_at_its_next_connection(self):
        super().test_a_box_taken_out_of_the_pair_is_refused_at_its_next_connection()

    def test_the_pair_port_opens_with_a_pair_and_closes_without(self):
        super().test_the_pair_port_opens_with_a_pair_and_closes_without()

    def test_a_certificate_that_is_not_the_one_pinned_leaves_the_box_alone(self):
        super().test_a_certificate_that_is_not_the_one_pinned_leaves_the_box_alone()

    def test_an_entrys_client_that_leaves_ends_the_request_on_the_other_box(self):
        super().test_an_entrys_client_that_leaves_ends_the_request_on_the_other_box()

    def test_an_answer_past_the_cap_is_refused(self):
        super().test_an_answer_past_the_cap_is_refused()

    def test_an_error_answer_is_relayed_whole_up_to_the_cap_and_refused_past_it(self):
        super().test_an_error_answer_is_relayed_whole_up_to_the_cap_and_refused_past_it()

    def test_an_error_answer_the_other_box_cuts_is_said_cut(self):
        super().test_an_error_answer_the_other_box_cuts_is_said_cut()

    def test_an_answer_the_other_box_cuts_is_said_cut(self):
        super().test_an_answer_the_other_box_cuts_is_said_cut()

    def test_a_certificate_whose_dates_this_clock_does_not_hold_still_pairs(self):
        super().test_a_certificate_whose_dates_this_clock_does_not_hold_still_pairs()

    def test_the_tile_reads_the_other_box_through_the_pair(self):
        super().test_the_tile_reads_the_other_box_through_the_pair()

    def test_the_tiles_route_is_this_machines_alone(self):
        super().test_the_tiles_route_is_this_machines_alone()

    def test_a_peer_whose_engine_restarts_gets_a_503_to_try_again_not_a_404(self):
        super().test_a_peer_whose_engine_restarts_gets_a_503_to_try_again_not_a_404()

    def test_a_box_never_read_holds_no_lane_past_its_first_read(self):
        super().test_a_box_never_read_holds_no_lane_past_its_first_read()

    def test_a_box_that_went_off_holds_its_lanes_for_a_while_then_none(self):
        super().test_a_box_that_went_off_holds_its_lanes_for_a_while_then_none()

    def test_a_box_that_lists_a_model_and_refuses_it_twice_gets_a_503_never_this_boxs_answer(self):
        super().test_a_box_that_lists_a_model_and_refuses_it_twice_gets_a_503_never_this_boxs_answer()

    def test_a_lane_the_box_that_does_not_answer_never_served_is_answered_here(self):
        super().test_a_lane_the_box_that_does_not_answer_never_served_is_answered_here()

    def test_a_stream_the_peer_cuts_reaches_the_client_cut_never_ended(self):
        super().test_a_stream_the_peer_cuts_reaches_the_client_cut_never_ended()

    def test_a_request_sent_on_to_a_box_whose_engine_restarts_is_told_to_try_again_not_held(self):
        super().test_a_request_sent_on_to_a_box_whose_engine_restarts_is_told_to_try_again_not_held()

    def test_a_peer_that_takes_the_connection_and_never_answers_holds_nobody(self):
        super().test_a_peer_that_takes_the_connection_and_never_answers_holds_nobody()

    def test_two_boxes_that_name_each_other_ask_each_other_once(self):
        super().test_two_boxes_that_name_each_other_ask_each_other_once()

    def test_a_box_that_cannot_read_its_own_key_sends_nothing_on(self):
        super().test_a_box_that_cannot_read_its_own_key_sends_nothing_on()

    def test_a_pair_json_with_a_bad_port_or_name_leaves_the_box_alone(self):
        super().test_a_pair_json_with_a_bad_port_or_name_leaves_the_box_alone()

    def test_an_entry_whose_engine_does_not_answer_says_so_on_v1_models(self):
        super().test_an_entry_whose_engine_does_not_answer_says_so_on_v1_models()

    def test_no_proxy_of_the_environment_gets_the_peers_key(self):
        super().test_no_proxy_of_the_environment_gets_the_peers_key()

    def test_a_peer_that_answers_409_has_every_list_read_again(self):
        super().test_a_peer_that_answers_409_has_every_list_read_again()

    def test_a_silent_peer_stream_reaches_the_client_cut(self):
        super().test_a_silent_peer_stream_reaches_the_client_cut()

    def test_a_request_held_through_a_restart_is_never_answered_by_another_lane(self):
        super().test_a_request_held_through_a_restart_is_never_answered_by_another_lane()


class EveryCaseHasItsInProcessTwin(unittest.TestCase):
    def test_the_twin_declares_every_case_of_its_class(self):
        # as tests/test_proxy_hold.py checks its own: four tests of the sharing were not declared in its twin (found on
        # 2026-11-04), which ran them by inheritance, so a twin that stops inheriting would drop them unseen
        twin = TwoBoxesBehindOneEndpointInProcess
        base = twin.__bases__[0]
        cases = {n for n in vars(base) if n.startswith("test_")}
        self.assertTrue(cases, base.__name__)
        self.assertEqual({n for n in vars(twin) if n.startswith("test_")}, cases, twin.__name__)


if __name__ == "__main__":
    unittest.main()
