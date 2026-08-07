from __future__ import annotations

import unittest

from wiki_links import extract_first_article_link, normalise_title


class NormaliseTitleTests(unittest.TestCase):
    def test_normalises_mediawiki_spacing_case_and_fragment(self) -> None:
        self.assertEqual(normalise_title(" philosophy_of_mind#History "), "Philosophy of mind")

    def test_rejects_non_article_namespaces_and_relative_targets(self) -> None:
        for target in (
            "File:Example.svg",
            "Category:Logic",
            "fr:Philosophie",
            ":fr:Philosophie",
            "#Section",
            "/Subpage",
        ):
            with self.subTest(target=target):
                self.assertIsNone(normalise_title(target))

    def test_keeps_mainspace_titles_with_colons(self) -> None:
        self.assertEqual(
            normalise_title("star_trek:_the_next_generation"),
            "Star trek: the next generation",
        )


class ExtractFirstArticleLinkTests(unittest.TestCase):
    def test_uses_first_eligible_lead_link(self) -> None:
        text = "{{Infobox|link=[[Ignored template]]}} [[File:Image.png]] A [[philosophy|discipline]] studies things."
        self.assertEqual(extract_first_article_link(text), "Philosophy")

    def test_ignores_parenthetical_and_reference_links(self) -> None:
        text = "A subject ([[Ignored]])<ref>[[Also ignored]]</ref> with [[Metaphysics]].\n== History ==\n[[Later]]"
        self.assertEqual(extract_first_article_link(text), "Metaphysics")

    def test_returns_none_when_no_mainspace_link_exists(self) -> None:
        self.assertIsNone(extract_first_article_link("[[Category:Nothing]] and [[File:None.png]]"))

    def test_stops_after_lead_section(self) -> None:
        self.assertIsNone(extract_first_article_link("No links here.\n== History ==\n[[Philosophy]]"))


if __name__ == "__main__":
    unittest.main()
