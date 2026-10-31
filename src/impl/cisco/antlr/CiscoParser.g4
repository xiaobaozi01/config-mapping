parser grammar CiscoParser;
options { tokenVocab=CiscoLexer; contextSuperClass=CiscoContext; }
@header {
from ..context import CiscoContext
}
configuration: node* EOF;
node
    : GROUP trivia* (INDENT node* DEDENT)? END_GROUP
    | LINE trivia* (INDENT node+ DEDENT)?
    | BANNER
    | SEPARATOR
    | COMMENT
    | BLANK
    ;

trivia: COMMENT | BLANK;
