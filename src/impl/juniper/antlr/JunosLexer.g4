lexer grammar JunosLexer;

LBRACE: '{';
RBRACE: '}';
SEMI: ';';
LINE_COMMENT: '#' ~[\r\n]*;
BLOCK_COMMENT: '/*' .*? '*/';
DOUBLE_QUOTED_STRING: '"' ('\\' . | ~["\\])* '"';
SINGLE_QUOTED_STRING: '\'' ('\\' . | ~['\\])* '\'';
TEXT: ~[ \t\r\n\uFEFF{};"'/#]+;
WHITESPACE: [ \t\r\n\uFEFF]+ -> channel(HIDDEN);
SLASH: '/';
OTHER: .;
