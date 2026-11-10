#!/usr/bin/env python3
"""Two boxes that serve one model, the same cases as tests/test_proxy_pair_one_model.py with the entry in this
process: what CI's coverage floor measures. Each case is declared again rather than only inherited: CI counts the
tests a file declares against the tests that ran, and an inherited case runs without being declared.

The pair's tests are four files since 2026-11-10, so that CI runs them side by side: this one,
tests/test_proxy_pair.py, tests/test_proxy_pair_inprocess.py and
tests/test_proxy_pair_one_model.py. What they share (the two
boxes, their identities, the fake engines, PairFixture) is tests/pairbox.py.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# the module and not its class: unittest runs every TestCase a module names, so the class named here would run
# its cases a second time in this file
import test_proxy_pair_one_model as one_model  # noqa: E402


class TwoBoxesServeOneModelInProcess(one_model.TwoBoxesServeOneModel):
    INPROC = True

    def test_a_conversation_stays_on_the_box_that_served_it(self):
        super().test_a_conversation_stays_on_the_box_that_served_it()

    def test_a_box_held_by_its_memory_floor_sends_on_what_the_other_box_serves(self):
        super().test_a_box_held_by_its_memory_floor_sends_on_what_the_other_box_serves()

    def test_a_box_gone_without_a_reset_holds_a_shared_request_seconds_not_ten(self):
        super().test_a_box_gone_without_a_reset_holds_a_shared_request_seconds_not_ten()

    def test_a_prompt_too_long_for_this_boxs_pool_goes_to_the_other_box(self):
        super().test_a_prompt_too_long_for_this_boxs_pool_goes_to_the_other_box()

    def test_a_long_prompt_a_warming_engine_cannot_take_goes_to_the_other_box(self):
        super().test_a_long_prompt_a_warming_engine_cannot_take_goes_to_the_other_box()

    def test_a_claude_code_conversation_stays_on_its_box(self):
        super().test_a_claude_code_conversation_stays_on_its_box()

    def test_boxes_far_apart_share_even_a_conversation(self):
        super().test_boxes_far_apart_share_even_a_conversation()

    def test_a_burst_of_new_conversations_is_shared(self):
        super().test_a_burst_of_new_conversations_is_shared()

    def test_a_box_that_refuses_hands_its_share_back(self):
        super().test_a_box_that_refuses_hands_its_share_back()

    def test_a_request_the_other_box_drops_runs_nowhere_else(self):
        super().test_a_request_the_other_box_drops_runs_nowhere_else()

    def test_the_tile_says_what_this_box_sent_the_other(self):
        super().test_the_tile_says_what_this_box_sent_the_other()

    def test_this_box_hands_a_request_its_engine_refuses_to_the_other(self):
        super().test_this_box_hands_a_request_its_engine_refuses_to_the_other()

    def test_a_box_that_said_529_gets_nothing_shared_for_a_while(self):
        super().test_a_box_that_said_529_gets_nothing_shared_for_a_while()

    def test_a_prompt_too_long_for_the_other_box_is_served_here_and_the_next_ones_still_go_there(self):
        super().test_a_prompt_too_long_for_the_other_box_is_served_here_and_the_next_ones_still_go_there()

    def test_a_box_that_switched_is_not_sent_the_next_request(self):
        super().test_a_box_that_switched_is_not_sent_the_next_request()

    def test_a_refusal_cut_on_the_way_is_still_served_here(self):
        super().test_a_refusal_cut_on_the_way_is_still_served_here()

    def test_a_list_read_long_ago_still_shares(self):
        super().test_a_list_read_long_ago_still_shares()

    def test_a_conversation_served_here_while_the_other_box_was_away_stays_here(self):
        super().test_a_conversation_served_here_while_the_other_box_was_away_stays_here()

    def test_a_token_count_stays_here_and_moves_no_conversation(self):
        super().test_a_token_count_stays_here_and_moves_no_conversation()

    def test_a_request_this_boxs_engine_died_on_goes_to_no_other_engine(self):
        super().test_a_request_this_boxs_engine_died_on_goes_to_no_other_engine()

    def test_the_other_box_whose_engine_died_on_a_request_says_502_and_it_goes_no_further(self):
        super().test_the_other_box_whose_engine_died_on_a_request_says_502_and_it_goes_no_further()

    def test_a_request_asked_of_this_box_is_never_shared_nor_handed_over(self):
        super().test_a_request_asked_of_this_box_is_never_shared_nor_handed_over()

    def test_a_box_that_does_not_say_what_it_runs_gets_nothing_shared(self):
        super().test_a_box_that_does_not_say_what_it_runs_gets_nothing_shared()

    def test_a_box_tells_the_other_how_many_it_runs_and_how_many_are_its(self):
        super().test_a_box_tells_the_other_how_many_it_runs_and_how_many_are_its()

    def test_the_tile_still_says_what_was_shared_when_the_other_box_does_not_answer(self):
        super().test_the_tile_still_says_what_was_shared_when_the_other_box_does_not_answer()

    def test_a_lane_of_16_keeps_a_conversation_5_ahead(self):
        super().test_a_lane_of_16_keeps_a_conversation_5_ahead()

    def test_the_gap_is_half_the_slots_the_other_box_says(self):
        super().test_the_gap_is_half_the_slots_the_other_box_says()

    def test_a_lane_whose_port_refuses_hands_its_request_to_the_other_box(self):
        super().test_a_lane_whose_port_refuses_hands_its_request_to_the_other_box()

    def test_a_request_kept_here_goes_over_when_the_floor_holds_this_box(self):
        super().test_a_request_kept_here_goes_over_when_the_floor_holds_this_box()

    def test_a_count_this_box_failed_is_no_engine_that_closed_on_a_request(self):
        super().test_a_count_this_box_failed_is_no_engine_that_closed_on_a_request()

    def test_a_hop_whose_engine_dies_in_an_open_stream_gets_an_error_event(self):
        super().test_a_hop_whose_engine_dies_in_an_open_stream_gets_an_error_event()

    def test_a_request_asked_of_this_box_goes_nowhere_else_while_its_engine_is_away(self):
        super().test_a_request_asked_of_this_box_goes_nowhere_else_while_its_engine_is_away()

    def test_a_request_this_boxs_engine_died_on_stays_here_when_sent_again(self):
        super().test_a_request_this_boxs_engine_died_on_stays_here_when_sent_again()

    def test_a_request_the_other_boxs_engine_died_on_goes_there_or_nowhere_when_sent_again(self):
        super().test_a_request_the_other_boxs_engine_died_on_goes_there_or_nowhere_when_sent_again()

    def test_a_box_serving_another_checkpoint_gets_nothing_shared(self):
        super().test_a_box_serving_another_checkpoint_gets_nothing_shared()

    def test_a_tool_call_the_other_box_served_is_watched_here_too(self):
        super().test_a_tool_call_the_other_box_served_is_watched_here_too()

    def test_the_other_boxs_list_reads_write_a_line_a_minute_while_this_engine_is_away(self):
        super().test_the_other_boxs_list_reads_write_a_line_a_minute_while_this_engine_is_away()

    def test_a_client_that_leaves_while_the_other_box_does_not_answer_still_pauses_it(self):
        super().test_a_client_that_leaves_while_the_other_box_does_not_answer_still_pauses_it()

    def test_a_refusal_served_here_is_counted_once_this_box_served_it(self):
        super().test_a_refusal_served_here_is_counted_once_this_box_served_it()

    def test_a_request_bound_to_a_box_now_serving_another_checkpoint_goes_nowhere(self):
        super().test_a_request_bound_to_a_box_now_serving_another_checkpoint_goes_nowhere()

    def test_a_request_bound_to_a_box_that_serves_another_model_now_gets_an_answer(self):
        super().test_a_request_bound_to_a_box_that_serves_another_model_now_gets_an_answer()

    def test_a_request_the_proxy_rewrites_is_bound_as_its_client_sends_it(self):
        super().test_a_request_the_proxy_rewrites_is_bound_as_its_client_sends_it()

    def test_an_engine_that_dies_during_its_answer_binds_the_request(self):
        super().test_an_engine_that_dies_during_its_answer_binds_the_request()

    def test_this_engine_away_a_model_its_lane_served_goes_to_no_other_checkpoint(self):
        super().test_this_engine_away_a_model_its_lane_served_goes_to_no_other_checkpoint()

    def test_an_ordered_stop_binds_no_request(self):
        super().test_an_ordered_stop_binds_no_request()

    def test_a_restart_binds_what_its_engine_closed_on(self):
        super().test_a_restart_binds_what_its_engine_closed_on()

    def test_a_hop_this_box_stops_by_order_answers_in_its_clients_dialect(self):
        super().test_a_hop_this_box_stops_by_order_answers_in_its_clients_dialect()

    def test_a_dead_vllm_engine_under_an_ordered_stop_is_said_to_be_one(self):
        super().test_a_dead_vllm_engine_under_an_ordered_stop_is_said_to_be_one()

    def test_a_request_bound_here_is_refused_when_the_others_list_failed_but_named_it(self):
        super().test_a_request_bound_here_is_refused_when_the_others_list_failed_but_named_it()

    def test_a_box_that_reads_no_key_holds_what_its_own_engine_will_serve(self):
        super().test_a_box_that_reads_no_key_holds_what_its_own_engine_will_serve()

    def test_a_hop_whose_engine_closes_as_its_lane_stops_by_order_gets_a_503_that_says_so(self):
        super().test_a_hop_whose_engine_closes_as_its_lane_stops_by_order_gets_a_503_that_says_so()

    def test_an_ordered_death_in_a_stream_the_keeper_opened_is_said_in_it(self):
        super().test_an_ordered_death_in_a_stream_the_keeper_opened_is_said_in_it()

    def test_a_death_the_other_box_relayed_before_its_client_left_is_bound(self):
        super().test_a_death_the_other_box_relayed_before_its_client_left_is_bound()

    def test_a_request_bound_here_is_answered_here_once_the_other_box_said_nothing_for_long(self):
        super().test_a_request_bound_here_is_answered_here_once_the_other_box_said_nothing_for_long()

    def test_a_box_that_reads_no_key_holds_its_own_lane_from_the_same_checkpoint_too(self):
        super().test_a_box_that_reads_no_key_holds_its_own_lane_from_the_same_checkpoint_too()

    def test_a_request_bound_here_is_answered_here_when_the_other_box_moved_to_another_model(self):
        super().test_a_request_bound_here_is_answered_here_when_the_other_box_moved_to_another_model()

    def test_a_death_relayed_then_an_order_said_after_it_binds_nothing(self):
        super().test_a_death_relayed_then_an_order_said_after_it_binds_nothing()

    def test_a_death_relayed_then_an_order_said_after_a_silence_binds_nothing(self):
        super().test_a_death_relayed_then_an_order_said_after_a_silence_binds_nothing()

    def test_a_stop_job_seen_after_a_vllm_death_is_said_before_the_streams_end(self):
        super().test_a_stop_job_seen_after_a_vllm_death_is_said_before_the_streams_end()

    def test_a_death_relayed_is_bound_at_once_when_its_client_leaves_and_that_box_goes_quiet(self):
        super().test_a_death_relayed_is_bound_at_once_when_its_client_leaves_and_that_box_goes_quiet()

    def test_a_death_here_whose_order_shows_at_the_streams_end_is_unbound(self):
        super().test_a_death_here_whose_order_shows_at_the_streams_end_is_unbound()

    def test_a_death_relayed_is_bound_as_it_is_relayed(self):
        super().test_a_death_relayed_is_bound_as_it_is_relayed()

    def test_an_order_line_split_where_its_client_leaves_is_read_whole(self):
        super().test_an_order_line_split_where_its_client_leaves_is_read_whole()

    def test_the_line_end_before_an_order_line_stays_at_the_entry(self):
        super().test_the_line_end_before_an_order_line_stays_at_the_entry()

    def test_an_ndjson_cut_is_bound_before_its_client_sees_it(self):
        super().test_an_ndjson_cut_is_bound_before_its_client_sees_it()

    def test_a_cut_during_a_floor_stop_is_bound_and_no_stop_counts_it(self):
        super().test_a_cut_during_a_floor_stop_is_bound_and_no_stop_counts_it()

    def test_a_death_the_other_box_sends_after_this_boxs_client_left_is_bound(self):
        super().test_a_death_the_other_box_sends_after_this_boxs_client_left_is_bound()

    def test_a_death_the_other_box_answers_after_this_boxs_client_left_is_bound(self):
        super().test_a_death_the_other_box_answers_after_this_boxs_client_left_is_bound()

    def test_a_box_that_closes_on_a_shared_request_after_its_client_left_is_paused(self):
        super().test_a_box_that_closes_on_a_shared_request_after_its_client_left_is_paused()

    def test_a_box_away_that_refuses_a_shared_request_after_its_client_left_is_paused(self):
        super().test_a_box_away_that_refuses_a_shared_request_after_its_client_left_is_paused()

    def test_a_box_that_refuses_a_shared_request_as_its_client_leaves_serves_it_nowhere(self):
        super().test_a_box_that_refuses_a_shared_request_as_its_client_leaves_serves_it_nowhere()

    def test_a_box_that_closes_on_a_request_as_its_client_leaves_is_read_with_its_client_gone(self):
        super().test_a_box_that_closes_on_a_request_as_its_client_leaves_is_read_with_its_client_gone()

    def test_a_refusal_as_its_client_leaves_moves_no_conversation_there(self):
        super().test_a_refusal_as_its_client_leaves_moves_no_conversation_there()

    def test_an_error_as_its_client_leaves_keeps_the_conversation_there(self):
        super().test_an_error_as_its_client_leaves_keeps_the_conversation_there()

    def test_an_answer_cut_as_its_client_leaves_is_read_with_its_client_gone(self):
        super().test_an_answer_cut_as_its_client_leaves_is_read_with_its_client_gone()

    def test_a_whole_answer_as_its_client_leaves_goes_to_nobody(self):
        super().test_a_whole_answer_as_its_client_leaves_goes_to_nobody()

    def test_a_hop_a_stop_marks_during_its_count_still_gets_its_400(self):
        super().test_a_hop_a_stop_marks_during_its_count_still_gets_its_400()

    def test_a_request_the_floor_stops_during_its_count_is_refused_by_it_never_handed_on(self):
        super().test_a_request_the_floor_stops_during_its_count_is_refused_by_it_never_handed_on()

    def test_a_request_a_stop_marks_during_its_count_is_never_handed_on_as_too_long(self):
        super().test_a_request_a_stop_marks_during_its_count_is_never_handed_on_as_too_long()

    def test_a_request_the_floor_stops_during_its_count_is_refused_by_the_floor(self):
        super().test_a_request_the_floor_stops_during_its_count_is_refused_by_the_floor()

    def test_an_ollama_stream_that_ends_whole_as_its_client_leaves_binds_nothing(self):
        super().test_an_ollama_stream_that_ends_whole_as_its_client_leaves_binds_nothing()

    def test_an_answer_that_comes_after_its_client_left_goes_to_nobody(self):
        super().test_an_answer_that_comes_after_its_client_left_goes_to_nobody()

    def test_a_request_a_stop_marks_during_its_count_is_cut_never_held_or_handed_on(self):
        super().test_a_request_a_stop_marks_during_its_count_is_cut_never_held_or_handed_on()

    def test_a_stream_the_other_box_begins_after_this_boxs_client_left_is_read_for_a_death(self):
        super().test_a_stream_the_other_box_begins_after_this_boxs_client_left_is_read_for_a_death()

    def test_a_stream_whose_head_this_box_could_not_write_is_read_for_a_death(self):
        super().test_a_stream_whose_head_this_box_could_not_write_is_read_for_a_death()

    def test_a_box_that_closes_on_a_request_after_its_client_left_is_bound(self):
        super().test_a_box_that_closes_on_a_request_after_its_client_left_is_bound()

    def test_an_answer_cut_after_its_client_left_is_bound(self):
        super().test_an_answer_cut_after_its_client_left_is_bound()

    def test_the_watchs_own_end_of_a_silent_box_binds_nothing(self):
        super().test_the_watchs_own_end_of_a_silent_box_binds_nothing()

    def test_the_watchs_own_end_of_an_answer_gone_silent_binds_nothing(self):
        super().test_the_watchs_own_end_of_an_answer_gone_silent_binds_nothing()

    def test_a_stream_that_ends_after_this_boxs_client_left_binds_nothing(self):
        super().test_a_stream_that_ends_after_this_boxs_client_left_binds_nothing()

    def test_this_boxs_own_end_of_the_connection_is_no_death_of_the_other_boxs_engine(self):
        super().test_this_boxs_own_end_of_the_connection_is_no_death_of_the_other_boxs_engine()

    def test_a_retry_that_bound_nothing_keeps_the_binding_a_death_before_it_made(self):
        super().test_a_retry_that_bound_nothing_keeps_the_binding_a_death_before_it_made()

    def test_a_try_ended_by_order_keeps_the_binding_its_first_try_made(self):
        super().test_a_try_ended_by_order_keeps_the_binding_its_first_try_made()

    def test_a_stop_during_a_dead_streams_reads_counts_nothing_and_holds_nothing_back(self):
        super().test_a_stop_during_a_dead_streams_reads_counts_nothing_and_holds_nothing_back()

    def test_a_hops_death_relayed_is_never_said_an_order_by_a_stop_after_it(self):
        super().test_a_hops_death_relayed_is_never_said_an_order_by_a_stop_after_it()

    def test_a_cut_under_an_order_binds_nothing_and_tells_the_hops_box(self):
        super().test_a_cut_under_an_order_binds_nothing_and_tells_the_hops_box()

    def test_a_death_whose_own_read_saw_the_order_stays_unbound_at_the_cut(self):
        super().test_a_death_whose_own_read_saw_the_order_stays_unbound_at_the_cut()

    def test_a_death_here_whose_client_leaves_before_its_order_shows_is_unbound(self):
        super().test_a_death_here_whose_client_leaves_before_its_order_shows_is_unbound()

    def test_a_cut_is_bound_before_its_client_sees_it(self):
        super().test_a_cut_is_bound_before_its_client_sees_it()

    def test_a_death_here_whose_order_shows_before_a_cut_is_unbound(self):
        super().test_a_death_here_whose_order_shows_before_a_cut_is_unbound()

    def test_a_recovery_restart_to_a_staged_checkpoint_binds_what_its_engine_closed_on(self):
        super().test_a_recovery_restart_to_a_staged_checkpoint_binds_what_its_engine_closed_on()

    def test_an_ordered_503_whose_body_was_cut_is_still_an_order(self):
        super().test_an_ordered_503_whose_body_was_cut_is_still_an_order()

    def test_a_death_the_other_box_said_was_ordered_after_it_binds_nothing(self):
        super().test_a_death_the_other_box_said_was_ordered_after_it_binds_nothing()

    def test_a_hop_this_box_stops_by_order_before_any_answer_gets_a_503_that_says_so(self):
        super().test_a_hop_this_box_stops_by_order_before_any_answer_gets_a_503_that_says_so()

    def test_a_hop_this_box_stops_by_order_in_its_open_stream_says_so_before_the_cut(self):
        super().test_a_hop_this_box_stops_by_order_in_its_open_stream_says_so_before_the_cut()

    def test_a_stream_the_other_box_ends_by_order_binds_nothing(self):
        super().test_a_stream_the_other_box_ends_by_order_binds_nothing()

    def test_a_503_the_other_box_says_is_an_order_is_not_run_here_nor_bound(self):
        super().test_a_503_the_other_box_says_is_an_order_is_not_run_here_nor_bound()

    def test_this_engine_starting_its_lane_goes_to_no_other_checkpoint(self):
        super().test_this_engine_starting_its_lane_goes_to_no_other_checkpoint()

    def test_a_request_a_dead_vllm_engine_answered_stays_here_when_sent_again(self):
        super().test_a_request_a_dead_vllm_engine_answered_stays_here_when_sent_again()

    def test_a_request_a_dead_vllm_engine_ended_in_its_stream_stays_here_when_sent_again(self):
        super().test_a_request_a_dead_vllm_engine_ended_in_its_stream_stays_here_when_sent_again()

    def test_a_request_the_other_boxs_dead_vllm_engine_answered_goes_there_or_nowhere(self):
        super().test_a_request_the_other_boxs_dead_vllm_engine_answered_goes_there_or_nowhere()

    def test_an_engine_that_closes_the_connection_on_the_count_closed_it_on_the_request(self):
        super().test_an_engine_that_closes_the_connection_on_the_count_closed_it_on_the_request()

    def test_a_request_bound_to_a_box_that_left_the_pair_goes_nowhere(self):
        super().test_a_request_bound_to_a_box_that_left_the_pair_goes_nowhere()

    def test_a_request_bound_here_for_a_name_no_box_serves_is_this_boxs_lanes(self):
        super().test_a_request_bound_here_for_a_name_no_box_serves_is_this_boxs_lanes()

    def test_a_request_bound_to_the_other_box_on_a_box_that_reads_no_key_runs_nowhere(self):
        super().test_a_request_bound_to_the_other_box_on_a_box_that_reads_no_key_runs_nowhere()

    def test_a_request_bound_to_a_box_of_a_pair_dissolved_since_goes_nowhere(self):
        super().test_a_request_bound_to_a_box_of_a_pair_dissolved_since_goes_nowhere()

    def test_a_request_bound_while_this_box_was_alone_is_this_boxs(self):
        super().test_a_request_bound_while_this_box_was_alone_is_this_boxs()

    def test_a_box_that_refuses_this_boxs_certificate_never_had_the_request(self):
        super().test_a_box_that_refuses_this_boxs_certificate_never_had_the_request()

    def test_a_client_that_leaves_while_a_box_that_took_its_request_reads_it_pauses_nothing(self):
        super().test_a_client_that_leaves_while_a_box_that_took_its_request_reads_it_pauses_nothing()

    def test_a_shared_request_carries_this_boxs_checkpoint_and_never_the_clients(self):
        super().test_a_shared_request_carries_this_boxs_checkpoint_and_never_the_clients()

    def test_a_request_for_a_model_only_the_other_box_serves_carries_no_checkpoint(self):
        super().test_a_request_for_a_model_only_the_other_box_serves_carries_no_checkpoint()

    def test_a_request_sent_on_for_another_checkpoint_than_this_boxs_is_refused(self):
        super().test_a_request_sent_on_for_another_checkpoint_than_this_boxs_is_refused()

    def test_a_box_that_serves_no_text_lane_now_sends_the_others_model_there(self):
        super().test_a_box_that_serves_no_text_lane_now_sends_the_others_model_there()

    def test_the_lane_seen_last_keeps_its_checkpoint_while_the_unit_says_none(self):
        super().test_the_lane_seen_last_keeps_its_checkpoint_while_the_unit_says_none()

    def test_another_lane_saying_no_checkpoint_is_noted_with_none(self):
        super().test_another_lane_saying_no_checkpoint_is_noted_with_none()

    def test_a_request_bound_to_a_box_whose_list_is_unknown_is_tried_there(self):
        super().test_a_request_bound_to_a_box_whose_list_is_unknown_is_tried_there()

    def test_a_request_bound_here_goes_nowhere_once_this_box_serves_another_model(self):
        super().test_a_request_bound_here_goes_nowhere_once_this_box_serves_another_model()


class EveryCaseHasItsInProcessTwin(unittest.TestCase):
    def test_the_twin_declares_every_case_of_its_class(self):
        # as tests/test_proxy_hold.py checks its own: four tests of the sharing were not declared in its twin (found on
        # 2026-11-04), which ran them by inheritance, so a twin that stops inheriting would drop them unseen
        twin = TwoBoxesServeOneModelInProcess
        base = twin.__bases__[0]
        cases = {n for n in vars(base) if n.startswith("test_")}
        self.assertTrue(cases, base.__name__)
        self.assertEqual({n for n in vars(twin) if n.startswith("test_")}, cases, twin.__name__)


if __name__ == "__main__":
    unittest.main()
