import unittest

from scripts.codex_tts_notify import select_text_for_tts


class SelectTextForTtsTests(unittest.TestCase):
    def test_uses_tts_summary_section_when_present(self) -> None:
        message = (
            "## TTS Summary\n"
            "Project completed successfully.\n"
            "Queue parser updated.\n"
            "\n"
            "## Details\n"
            "- Full implementation notes"
        )

        selected, used_summary = select_text_for_tts(message)

        self.assertTrue(used_summary)
        self.assertEqual(selected, "Project completed successfully.\nQueue parser updated.")

    def test_falls_back_to_full_message_when_summary_missing(self) -> None:
        message = "## Details\nDetailed text only."

        selected, used_summary = select_text_for_tts(message)

        self.assertFalse(used_summary)
        self.assertEqual(selected, message)

    def test_falls_back_to_full_message_when_summary_is_empty(self) -> None:
        message = "## TTS Summary\n\n## Details\nDetailed text only."

        selected, used_summary = select_text_for_tts(message)

        self.assertFalse(used_summary)
        self.assertEqual(selected, message)

    def test_falls_back_to_full_message_when_summary_has_no_body(self) -> None:
        message = "## TTS Summary"

        selected, used_summary = select_text_for_tts(message)

        self.assertFalse(used_summary)
        self.assertEqual(selected, message)

    def test_stops_at_next_level_two_heading(self) -> None:
        message = (
            "## TTS Summary\n"
            "Speak this sentence.\n"
            "## Details\n"
            "Do not speak this part.\n"
            "## Extra\n"
            "Also do not speak this."
        )

        selected, used_summary = select_text_for_tts(message)

        self.assertTrue(used_summary)
        self.assertEqual(selected, "Speak this sentence.")

    def test_heading_match_is_case_insensitive(self) -> None:
        message = (
            "  ## tTs SuMmArY\n"
            "Keep this.\n"
            "## Details\n"
            "Ignore this."
        )

        selected, used_summary = select_text_for_tts(message)

        self.assertTrue(used_summary)
        self.assertEqual(selected, "Keep this.")


if __name__ == "__main__":
    unittest.main()
