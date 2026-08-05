"""Query understanding.

Routing decides retrieval shape and scope before anything is embedded. Two
behaviours matter most: a two-city question must fan out rather than run one
blended search, and a bare "Langley" must stop and ask rather than pick.
"""

from __future__ import annotations

import pytest

from app.db.enums import QueryIntent
from app.domain.query_router import QueryRouter


@pytest.fixture
def router() -> QueryRouter:
    return QueryRouter()


class TestSingleCity:
    @pytest.mark.parametrize(
        "question",
        [
            "Can I install a fascia sign in Coquitlam?",
            "Does Burnaby require permits for window graphics?",
            "What is the maximum sign area in Vancouver?",
        ],
    )
    def test_brief_examples_route_to_single_city(
        self, router: QueryRouter, question: str
    ) -> None:
        plan = router.route(question)
        assert plan.intent is QueryIntent.SINGLE_CITY
        assert len(plan.municipalities) == 1

    def test_municipality_is_resolved(self, router: QueryRouter) -> None:
        plan = router.route("Can I install a fascia sign in Coquitlam?")
        assert plan.municipality_slugs == ("coquitlam",)

    def test_sign_types_are_extracted(self, router: QueryRouter) -> None:
        plan = router.route("Can I install a fascia sign in Coquitlam?")
        assert "fascia" in plan.sign_types


class TestComparison:
    def test_two_cities_fan_out(self, router: QueryRouter) -> None:
        # A single blended search returns whichever city's wording matches
        # better, producing a lopsided comparison.
        plan = router.route("Compare Surrey and Richmond temporary sign regulations.")
        assert plan.intent is QueryIntent.MULTI_CITY_COMPARE
        assert set(plan.municipality_slugs) == {"surrey", "richmond"}
        assert plan.is_comparison

    def test_two_cities_without_a_comparison_word(self, router: QueryRouter) -> None:
        plan = router.route("Surrey and Richmond temporary sign rules")
        assert plan.intent is QueryIntent.MULTI_CITY_COMPARE

    def test_qualified_langleys_resolve_separately(self, router: QueryRouter) -> None:
        plan = router.route(
            "Compare sign regulations between the City of Langley and the "
            "Township of Langley."
        )
        assert plan.intent is QueryIntent.MULTI_CITY_COMPARE
        assert set(plan.municipality_slugs) == {"langley-city", "langley-township"}
        assert not plan.needs_clarification

    def test_versus_phrasing(self, router: QueryRouter) -> None:
        plan = router.route("Burnaby vs Vancouver banner rules")
        assert plan.intent is QueryIntent.MULTI_CITY_COMPARE


class TestAmbiguity:
    def test_bare_langley_asks_for_clarification(self, router: QueryRouter) -> None:
        plan = router.route("What are the sign rules in Langley?")
        assert plan.needs_clarification
        assert "Langley" in (plan.clarification_prompt() or "")

    def test_bare_north_vancouver_asks(self, router: QueryRouter) -> None:
        plan = router.route("Can I put up a banner in North Vancouver?")
        assert plan.needs_clarification

    def test_qualified_form_does_not_ask(self, router: QueryRouter) -> None:
        plan = router.route("Sign rules in the Township of Langley?")
        assert not plan.needs_clarification

    def test_unambiguous_city_does_not_ask(self, router: QueryRouter) -> None:
        assert not router.route("Sign rules in Burnaby?").needs_clarification


class TestOutOfScope:
    @pytest.mark.parametrize(
        "question",
        [
            "What is the weather in Vancouver today?",
            "What is the population of Surrey?",
            "Recommend a restaurant in Burnaby",
            "Tell me a joke",
        ],
    )
    def test_non_bylaw_questions_are_rejected(
        self, router: QueryRouter, question: str
    ) -> None:
        plan = router.route(question)
        assert plan.intent is QueryIntent.OUT_OF_SCOPE
        assert not plan.should_retrieve

    def test_naming_a_city_is_not_enough(self, router: QueryRouter) -> None:
        # The dangerous shape: a factual question the model could answer from
        # memory, about a municipality that is in the corpus.
        assert router.route("Surrey").intent is QueryIntent.OUT_OF_SCOPE

    def test_empty_query(self, router: QueryRouter) -> None:
        assert router.route("   ").intent is QueryIntent.OUT_OF_SCOPE

    def test_sign_vocabulary_keeps_it_in_scope(self, router: QueryRouter) -> None:
        assert router.route("fascia sign height limits").should_retrieve


class TestDefinitions:
    @pytest.mark.parametrize(
        "question",
        ["What counts as a fascia sign?", "Define projecting sign", "What is a banner?"],
    )
    def test_definition_questions(self, router: QueryRouter, question: str) -> None:
        assert router.route(question).intent is QueryIntent.DEFINITION

    def test_definition_with_a_city_prefers_the_city(self, router: QueryRouter) -> None:
        # Scoping matters more than the lookup shape: definitions differ by city.
        plan = router.route("What is a fascia sign in Coquitlam?")
        assert plan.intent is QueryIntent.SINGLE_CITY
        assert plan.municipality_slugs == ("coquitlam",)


class TestKeyword:
    def test_domain_question_without_a_city(self, router: QueryRouter) -> None:
        plan = router.route("Which bylaws restrict illuminated signs near highways?")
        assert plan.intent is QueryIntent.KEYWORD
        assert plan.municipalities == ()


class TestZones:
    def test_zoning_district_is_extracted(self, router: QueryRouter) -> None:
        # A question naming a zone can be answered precisely; one that does not
        # usually cannot, because the answer is a table.
        plan = router.route("Maximum sign area in the C-2 zone in Vancouver?")
        assert "C-2" in plan.zones

    def test_bylaw_numbers_are_not_zones(self, router: QueryRouter) -> None:
        plan = router.route("What does sign bylaw 4451 say about awnings?")
        assert plan.zones == ()


class TestPlanReporting:
    def test_reason_is_populated(self, router: QueryRouter) -> None:
        assert router.route("fascia signs in Burnaby").reason

    def test_clarification_prompt_is_none_when_unambiguous(
        self, router: QueryRouter
    ) -> None:
        assert router.route("fascia signs in Burnaby").clarification_prompt() is None
