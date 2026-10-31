"""Unit tests for :mod:`tests.ldbc.replay` (no network, no database)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyoxigraph as ox
import pytest

from tests.ldbc import replay

SN, SNVOC, XSD = replay.SN, replay.SNVOC, replay.XSD

#: A city, a country, a tag, a university and a company. Their ``snvoc:id``
#: values collide across kinds, as LDBC's do, so an insert that finds the
#: wrong kind would show.
_MINI = f"""
@prefix snvoc: <{SNVOC}> .
@prefix dbo: <{replay.DBO}> .
@prefix xsd: <{XSD}> .
<{SN}city5> a dbo:City ; snvoc:id "5"^^xsd:int .
<{SN}country5> a dbo:Country ; snvoc:id "5"^^xsd:int .
<{replay.TAG_PREFIX}Foo> snvoc:id "5"^^xsd:int .
<{SN}uni7> a dbo:University ; snvoc:id "7"^^xsd:int .
<{SN}comp7> a dbo:Company ; snvoc:id "7"^^xsd:int .
"""

_BIRTHDAY = round(datetime(1990, 1, 2, tzinfo=UTC).timestamp() * 1000)
_CREATED = round(datetime(2010, 1, 1, 0, 0, 0, 123000, tzinfo=UTC).timestamp() * 1000)

_IU1 = {
    "personId": 1,
    "personFirstName": "Ana",
    "personLastName": "Diaz",
    "gender": "female",
    "birthday": _BIRTHDAY,
    "creationDate": _CREATED,
    "locationIp": "1.2.3.4",
    "browserUsed": "Firefox",
    "cityId": 5,
    "languages": ["es"],
    "emails": ["ana@example.org"],
    "tagIds": [5],
    "studyAt": [[7, 2008]],
    "workAt": [[7, 2011]],
}
_IS1_ANSWER = {
    "firstName": "Ana",
    "lastName": "Diaz",
    "birthday": _BIRTHDAY,
    "locationIp": "1.2.3.4",
    "browserUsed": "Firefox",
    "cityId": 5,
    "gender": "female",
    "creationDate": _CREATED,
}


def _store() -> ox.Store:
    store = ox.Store()
    store.load(_MINI.encode(), format=ox.RdfFormat.TURTLE)
    return store


def _op(index: int, params: dict[str, Any], expected: Any = None) -> replay.Operation:
    return replay.Operation(index, replay.classify(params), params, expected)


def _apply(store: ox.Store, op: replay.Operation, nodes: replay._Nodes | None = None) -> None:
    for request in replay.update_sparql(op, nodes or replay._Nodes()):
        store.update(replay._PREFIXES + request)


def _objects(store: ox.Store, subject: str, predicate: str) -> set[str]:
    return {
        q.object.value
        for q in store.quads_for_pattern(ox.NamedNode(subject), ox.NamedNode(SNVOC + predicate), None)
    }


def test_classify_names_short_reads_complex_reads_and_inserts() -> None:
    assert replay.classify({"personIdSQ1": 1}) == "IS1"
    assert replay.classify({"messageForumId": 1}) == "IS6"
    assert replay.classify({"personIdQ2": 1, "maxDate": 0}) == "IC2"
    assert replay.classify({"person1IdQ13StartNode": 1, "person2IdQ13EndNode": 2}) == "IC13"
    assert replay.classify(_IU1) == "IU1"
    assert replay.classify({"person1Id": 1, "person2Id": 2, "creationDate": 0}) == "IU8"


def test_classify_refuses_a_key_set_it_does_not_know() -> None:
    with pytest.raises(replay.ReplayError, match="unrecognised operation with keys"):
        replay.classify({"personId": 1, "postId": 2})


def test_lines_split_into_parameters_and_answer_and_a_line_without_a_bar_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "v.csv"
    path.write_text(json.dumps({"personIdSQ1": 1}) + "|" + json.dumps(_IS1_ANSWER) + "\n", encoding="utf-8")
    [op] = replay.read_operations(path)
    assert (op.index, op.name, op.params, op.expected) == (0, "IS1", {"personIdSQ1": 1}, _IS1_ANSWER)

    path.write_text(json.dumps({"personIdSQ1": 1}) + "\n", encoding="utf-8")
    with pytest.raises(replay.ReplayError, match="line 1: no '|'"):
        list(replay.read_operations(path))


def test_a_new_person_gets_new_study_and_work_nodes_and_is1_reads_it_back() -> None:
    store = _store()
    _apply(store, _op(0, _IU1))
    person = f"{SN}pers{1:020d}"
    assert _objects(store, person, "isLocatedIn") == {SN + "city5"}
    assert _objects(store, person, "hasTag") == {replay.TAG_PREFIX + "Foo"}
    [study] = _objects(store, person, "studyAt")
    [work] = _objects(store, person, "workAt")
    assert study.startswith(replay.GENID + "study") and work.startswith(replay.GENID + "work")
    assert _objects(store, study, "hasOrganisation") == {SN + "uni7"}
    assert _objects(store, work, "hasOrganisation") == {SN + "comp7"}

    agrees, ours, theirs = replay.run_read(store, _op(1, {"personIdSQ1": 1}, _IS1_ANSWER))
    assert agrees, (ours, theirs)


def test_a_friendship_adds_its_node_and_direct_knows_both_ways() -> None:
    store = _store()
    _apply(store, _op(0, {"person1Id": 1, "person2Id": 2, "creationDate": _CREATED}))
    a, b = f"{SN}pers{1:020d}", f"{SN}pers{2:020d}"
    [node] = _objects(store, a, "knows")
    assert _objects(store, node, "hasPerson") == {b}
    assert _objects(store, a, "directKnows") == {b}
    assert _objects(store, b, "directKnows") == {a}


def test_a_comment_replying_to_a_comment_points_at_that_comment() -> None:
    store = _store()
    params = {
        "commentId": 9,
        "creationDate": _CREATED,
        "locationIp": "1.2.3.4",
        "browserUsed": "Chrome",
        "content": "yes",
        "length": 3,
        "authorPersonId": 1,
        "countryId": 5,
        "replyToPostId": -1,
        "replyToCommentId": 8,
        "tagIds": [],
    }
    _apply(store, _op(0, params))
    comment = f"{SN}comm{9:020d}"
    assert _objects(store, comment, "replyOf") == {f"{SN}comm{8:020d}"}
    assert _objects(store, comment, "isLocatedIn") == {SN + "country5"}
    assert _objects(store, comment, "content") == {"yes"}


def test_terms_normalise_to_ldbc_values() -> None:
    assert replay.normalise(ox.NamedNode(f"{SN}pers{42:020d}")) == 42
    assert replay.normalise(ox.NamedNode(SN + "city5")) == SN + "city5"
    stamp = ox.Literal("2010-01-01T00:00:00.123Z", datatype=ox.NamedNode(XSD + "dateTime"))
    assert replay.normalise(stamp) == _CREATED
    assert replay.normalise(ox.Literal("1990-01-02", datatype=ox.NamedNode(XSD + "date"))) == _BIRTHDAY
    assert replay.normalise(ox.Literal("7", datatype=ox.NamedNode(XSD + "long"))) == 7
    assert replay.normalise(ox.Literal("true")) is True
    assert replay.normalise(None) is None


def test_each_read_sees_only_the_inserts_before_it_and_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    wrong = dict(_IS1_ANSWER, firstName="Bea")
    ops = [
        _op(0, {"personIdSQ1": 1}, {}),  # before the insert: LDBC answers {} for no row
        _op(1, _IU1),
        _op(2, {"personIdSQ1": 1}, _IS1_ANSWER),
        _op(3, {"personIdSQ1": 1}, wrong),  # over --per-op: not checked
    ]
    with caplog.at_level(logging.INFO, logger=replay.__name__):
        report = replay.replay(_store(), iter(ops), ("IS1",), per_op=2)
    assert report.updates_applied == {"IU1": 1}
    assert (report.checked["IS1"], report.agreed["IS1"]) == (2, 2)
    assert report.disagreements == []
    assert "line 3: IS1 agrees" in caplog.text
    assert "line 4" not in caplog.text


def test_a_disagreement_keeps_its_line_and_both_answers() -> None:
    wrong = dict(_IS1_ANSWER, firstName="Bea")
    report = replay.replay(
        _store(), iter([_op(0, _IU1), _op(1, {"personIdSQ1": 1}, wrong)]), ("IS1",), per_op=5
    )
    assert (report.checked["IS1"], report.agreed["IS1"]) == (1, 0)
    [item] = report.disagreements
    assert (item["line"], item["op"]) == (2, "IS1")
    assert item["ours"][0][0] == "Ana" and item["ldbc"][0][0] == "Bea"


def test_an_answer_without_a_mapped_field_is_refused_not_read_as_null() -> None:
    # IS7's knows flag was mapped as ``replyAuthorKnows…`` (LDBC's key is
    # ``isReplyAuthorKnows…``): every row read null and disagreed.
    answer = {k: v for k, v in _IS1_ANSWER.items() if k != "gender"}
    store = _store()
    _apply(store, _op(0, _IU1))
    with pytest.raises(replay.ReplayError, match=r"line 2: IS1 answer has no field \['gender'\]"):
        replay.run_read(store, _op(1, {"personIdSQ1": 1}, answer))


def test_the_mapped_fields_are_the_ones_ldbc_publishes() -> None:
    """Every answer field each read maps, against LDBC's file (when present)."""
    try:
        path = replay.validation_file("0.1")
    except replay.ReplayError:
        pytest.skip("LDBC's SF0.1 validation parameters are not unpacked")
    seen: dict[str, set[str]] = {}
    for op in replay.read_operations(path):
        if op.name in replay.READS and op.name not in seen:
            answers = op.expected if isinstance(op.expected, list) else [op.expected]
            if answers and answers[0]:
                seen[op.name] = set(answers[0])
        if len(seen) == len(replay.READS):
            break
    assert {name: {ldbc for ldbc, _ in replay._READS[name][2]} for name in replay.READS} == seen


def test_the_command_leaves_is6_out_unless_asked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # IS6 hung a 50-per-op run for a day: --op was the only way to skip it.
    path = tmp_path / "v.csv"
    path.write_text(json.dumps({"personIdSQ1": 1}) + "|[]\n", encoding="utf-8")
    monkeypatch.setattr(replay, "load_store", lambda sf: _store())
    monkeypatch.setattr(replay, "validation_file", lambda sf: path)
    seen: list[tuple[str, ...]] = []
    real = replay.replay

    def spy(
        store: ox.Store,
        operations: Any,
        reads: tuple[str, ...],
        per_op: int,
        keep: int = 5,
    ) -> replay.ReplayReport:
        seen.append(reads)
        return real(store, operations, reads, per_op, keep)

    monkeypatch.setattr(replay, "replay", spy)
    assert replay.main(["--per-op", "1"]) == 0
    assert replay.main(["--per-op", "1", "--op", "IS6"]) == 0
    assert seen == [replay.DEFAULT_READS, ("IS6",)]
    assert "IS6" not in replay.DEFAULT_READS and set(replay.DEFAULT_READS) | {"IS6"} == set(replay.READS)
