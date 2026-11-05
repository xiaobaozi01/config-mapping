"""验证 Junos 省略分号时的边界、注释和规范输出。"""

import pytest

from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.document import (
    JunosDocument,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.syntax.parser import (
    JuniperParser,
    JunosConfigurationParseError,
)
from gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.syntax.writer import (
    JuniperWriter,
)


@pytest.mark.parametrize("indent", ["", "    ", "\t"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_newlines_separate_missing_and_explicit_semicolons(indent, newline):
    source = newline.join(
        [
            "system {",
            indent + "xxx *",
            indent + "host-name lab;",
            indent + "domain-name example",
            "}",
        ]
    )
    root = JuniperParser().parse(source)

    assert [node.header for node in root.nodes[0].nodes] == [
        "xxx *",
        "host-name lab",
        "domain-name example",
    ]
    assert [node.location.line for node in root.nodes[0].nodes] == [2, 3, 4]


@pytest.mark.parametrize("source", ["xxx *", "xxx *\n", "xxx * # last note"])
def test_end_of_file_terminates_statement(source):
    root = JuniperParser().parse(source)

    assert len(root.nodes) == 1
    assert root.nodes[0].header == "xxx *"
    if "#" in source:
        assert root.nodes[0].comment == "# last note"


def test_closing_brace_terminates_inline_statement():
    root = JuniperParser().parse("system { services { ssh } host-name lab }")

    assert root.nodes[0].nodes[0].nodes[0].header == "ssh"
    assert root.nodes[0].nodes[1].header == "host-name lab"


def test_line_comments_are_separate_from_headers_and_keep_delimiters_literal():
    root = JuniperParser().parse(
        "system { # block { ; }\n"
        "host-name lab # trailing } ; {\n"
        "# standalone { ; }\n"
        "domain-name example # final note\n"
        "}\n"
    )
    system = root.nodes[0]
    assert system.comment == "# block { ; }"
    assert system.nodes[0].header == "host-name lab"
    assert system.nodes[0].comment == "# trailing } ; {"
    assert system.nodes[1].is_comment_only
    assert system.nodes[1].comment == "# standalone { ; }"
    assert system.nodes[2].header == "domain-name example"
    assert system.nodes[2].comment == "# final note"


def test_multiline_block_comments_do_not_change_structure_or_join_commands():
    root = JuniperParser().parse(
        "system {\n"
        "/* standalone { ;\n } */\n"
        "host-name lab /* trailing { ;\n } */\n"
        "domain-name /* middle } */example\n"
        "}\n"
    )
    nodes = root.nodes[0].nodes
    assert nodes[0].is_comment_only
    assert nodes[0].comment == "/* standalone { ;\n } */"
    assert nodes[1].header == "host-name lab"
    assert nodes[1].comment == "/* trailing { ;\n } */"
    assert nodes[2].header.split() == ["domain-name", "example"]
    assert nodes[2].comment == "/* middle } */"
    assert nodes[2].location.line == 6


def test_quotes_keep_newlines_and_comment_markers_literal():
    root = JuniperParser().parse(
        "interfaces { ge-0/0/0 {\n"
        'description "literal { ; } # /* [ ] \\" quote\nsecond line"\n'
        "mtu 9000\n} }"
    )
    nodes = root.nodes[0].nodes[0].nodes
    assert len(nodes) == 2
    assert nodes[0].header == (
        'description "literal { ; } # /* [ ] \\" quote\nsecond line"'
    )
    assert nodes[0].comment is None
    assert nodes[1].header == "mtu 9000"


def test_multiline_lists_continue_with_comments_and_optional_semicolon():
    root = JuniperParser().parse(
        "system {\n"
        "name-server [\n"
        "192.0.2.1 # preferred } ;\n"
        "/* backup { */ 192.0.2.2\n"
        "]\n"
        "host-name lab\n"
        "}"
    )
    nodes = root.nodes[0].nodes
    assert len(nodes) == 3
    assert nodes[0].header.split() == [
        "name-server",
        "[",
        "192.0.2.1",
        "192.0.2.2",
        "]",
    ]
    assert nodes[0].comment == "# preferred } ;"
    assert nodes[1].is_comment_only
    assert nodes[1].comment == "/* backup { */"
    assert nodes[2].header == "host-name lab"


def test_block_openers_and_semicolons_on_following_lines_allow_comments():
    root = JuniperParser().parse(
        "system # block note\n/* before opening */\n{\n"
        "host-name lab # host note\n;\n"
        "services\n{ ssh }\n}"
    )
    system = root.nodes[0]
    assert system.is_block
    assert system.header == "system"
    assert system.comment == "# block note"
    assert system.nodes[0].is_comment_only
    assert system.nodes[0].comment == "/* before opening */"
    assert system.nodes[1].header == "host-name lab"
    assert system.nodes[1].comment == "# host note"
    assert system.nodes[2].is_block
    assert system.nodes[2].nodes[0].header == "ssh"


@pytest.mark.parametrize(
    "source, message",
    [
        ("system {\nhost-name lab\n}\n}", "第 4 行出现多余右大括号"),
        ("system {\nhost-name lab", "大括号不平衡"),
        ('description "unclosed', "unclosed quote or comment"),
        ("xxx * /* unclosed", "unclosed quote or comment"),
        ("name-server [\n192.0.2.1", "方括号不平衡"),
        ("name-server ]", "方括号不平衡"),
    ],
)
def test_malformed_regions_still_fail_with_original_locations(source, message):
    with pytest.raises(JunosConfigurationParseError, match=message):
        JuniperParser().parse(source)


def test_document_output_adds_semicolons_and_preserves_comments_on_reparse():
    document = JunosDocument(
        "system {\n"
        "host-name lab # hostname\n"
        "/* standalone } ; */\n"
        "domain-name example /* domain */ # note\n"
        "services { ssh }\n"
        "}\n"
    )
    rendered = document.render()
    assert "host-name lab; # hostname" in rendered
    assert "domain-name example; /* domain */\n    # note" in rendered
    assert "/* standalone } ; */" in rendered
    assert "ssh;" in rendered
    reparsed = JunosDocument(rendered)
    assert reparsed.hostname() == "lab"
    assert reparsed.render() == rendered
    for comment in (
        "# hostname",
        "/* standalone } ; */",
        "/* domain */",
        "# note",
    ):
        assert comment in reparsed.render()


def test_comments_within_multiline_list_survive_writer_roundtrip():
    source = "name-server [\n192.0.2.1 # one\n192.0.2.2 # two\n]\n"
    parser, writer = JuniperParser(), JuniperWriter()
    root = parser.parse(source)
    rendered = writer.write(root)
    reparsed = parser.parse(rendered)

    statements = [node for node in reparsed.nodes if not node.is_comment_only]
    assert len(statements) == 1
    assert statements[0].header == root.nodes[0].header
    assert statements[0].comment == "# one"
    assert reparsed.nodes[1].comment == "# two"
    assert writer.write(reparsed) == rendered


def test_nested_list_comments_and_block_contents_have_stable_output():
    source = (
        "system {\n"
        "name-server [\n192.0.2.1 # one\n192.0.2.2 # two\n]\n"
        "host-name lab /* first\n # internal block text\n*/ # after block\n"
        "}"
    )
    parser, writer = JuniperParser(), JuniperWriter()
    rendered = writer.write(parser.parse(source))
    assert "\n    # two" in rendered
    assert "/* first\n # internal block text\n*/" in rendered
    assert writer.write(parser.parse(rendered)) == rendered
    rendered = JunosDocument(source).render()
    assert JunosDocument(rendered).render() == rendered
