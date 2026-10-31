lexer grammar CiscoLexer;

tokens { INDENT, DEDENT }
GROUP: [ \t]* [gG][rR][oO][uU][pP] [ \t]+ ~[\r\n]* EOL;
END_GROUP: [ \t]* [eE][nN][dD] '-' [gG][rR][oO][uU][pP] [ \t]* EOL;
BANNER: [ \t]* [bB][aA][nN][nN][eE][rR] [ \t]+ ~[\r\n]* EOL;
SEPARATOR: [ \t]* '!' [ \t]* EOL;
COMMENT: [ \t]* '!' ~[\r\n]* EOL;
BLANK: [ \t]* ('\r\n' | '\n' | '\r');
LINE: ~[\r\n]+ EOL;
fragment EOL: '\r\n' | '\n' | '\r' | EOF;
