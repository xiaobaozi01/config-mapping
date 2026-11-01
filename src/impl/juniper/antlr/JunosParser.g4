parser grammar JunosParser;

options {
    tokenVocab=JunosLexer;
    contextSuperClass=JunosContext;
}

@header {
from ..context import JunosContext
}

configuration: element* EOF;

element
    : statement
    | LINE_COMMENT
    | BLOCK_COMMENT
    ;

statement
    : command
      (
        SEMI
        | LBRACE element* RBRACE SEMI?
      )
      ({self._input.LT(-1).line == self._input.LT(1).line}? LINE_COMMENT)?
    ;

command: commandPart+;

commandPart
    : TEXT
    | DOUBLE_QUOTED_STRING
    | SINGLE_QUOTED_STRING
    | BLOCK_COMMENT
    | SLASH
    | OTHER
    ;
