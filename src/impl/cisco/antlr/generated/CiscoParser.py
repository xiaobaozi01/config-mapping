# Generated from CiscoParser.g4 by ANTLR 4.13.2
# encoding: utf-8
from antlr4 import *
from io import StringIO
import sys
if sys.version_info[1] > 5:
	from typing import TextIO
else:
	from typing.io import TextIO


from ..context import CiscoContext

def serializedATN():
    return [
        4,1,9,58,2,0,7,0,2,1,7,1,2,2,7,2,1,0,5,0,8,8,0,10,0,12,0,11,9,0,
        1,0,1,0,1,1,1,1,5,1,17,8,1,10,1,12,1,20,9,1,1,1,1,1,5,1,24,8,1,10,
        1,12,1,27,9,1,1,1,3,1,30,8,1,1,1,1,1,1,1,5,1,35,8,1,10,1,12,1,38,
        9,1,1,1,1,1,4,1,42,8,1,11,1,12,1,43,1,1,1,1,3,1,48,8,1,1,1,1,1,1,
        1,1,1,3,1,54,8,1,1,2,1,2,1,2,0,0,3,0,2,4,0,1,1,0,7,8,66,0,9,1,0,
        0,0,2,53,1,0,0,0,4,55,1,0,0,0,6,8,3,2,1,0,7,6,1,0,0,0,8,11,1,0,0,
        0,9,7,1,0,0,0,9,10,1,0,0,0,10,12,1,0,0,0,11,9,1,0,0,0,12,13,5,0,
        0,1,13,1,1,0,0,0,14,18,5,3,0,0,15,17,3,4,2,0,16,15,1,0,0,0,17,20,
        1,0,0,0,18,16,1,0,0,0,18,19,1,0,0,0,19,29,1,0,0,0,20,18,1,0,0,0,
        21,25,5,1,0,0,22,24,3,2,1,0,23,22,1,0,0,0,24,27,1,0,0,0,25,23,1,
        0,0,0,25,26,1,0,0,0,26,28,1,0,0,0,27,25,1,0,0,0,28,30,5,2,0,0,29,
        21,1,0,0,0,29,30,1,0,0,0,30,31,1,0,0,0,31,54,5,4,0,0,32,36,5,9,0,
        0,33,35,3,4,2,0,34,33,1,0,0,0,35,38,1,0,0,0,36,34,1,0,0,0,36,37,
        1,0,0,0,37,47,1,0,0,0,38,36,1,0,0,0,39,41,5,1,0,0,40,42,3,2,1,0,
        41,40,1,0,0,0,42,43,1,0,0,0,43,41,1,0,0,0,43,44,1,0,0,0,44,45,1,
        0,0,0,45,46,5,2,0,0,46,48,1,0,0,0,47,39,1,0,0,0,47,48,1,0,0,0,48,
        54,1,0,0,0,49,54,5,5,0,0,50,54,5,6,0,0,51,54,5,7,0,0,52,54,5,8,0,
        0,53,14,1,0,0,0,53,32,1,0,0,0,53,49,1,0,0,0,53,50,1,0,0,0,53,51,
        1,0,0,0,53,52,1,0,0,0,54,3,1,0,0,0,55,56,7,0,0,0,56,5,1,0,0,0,8,
        9,18,25,29,36,43,47,53
    ]

class CiscoParser ( Parser ):

    grammarFileName = "CiscoParser.g4"

    atn = ATNDeserializer().deserialize(serializedATN())

    decisionsToDFA = [ DFA(ds, i) for i, ds in enumerate(atn.decisionToState) ]

    sharedContextCache = PredictionContextCache()

    literalNames = [  ]

    symbolicNames = [ "<INVALID>", "INDENT", "DEDENT", "GROUP", "END_GROUP", 
                      "BANNER", "SEPARATOR", "COMMENT", "BLANK", "LINE" ]

    RULE_configuration = 0
    RULE_node = 1
    RULE_trivia = 2

    ruleNames =  [ "configuration", "node", "trivia" ]

    EOF = Token.EOF
    INDENT=1
    DEDENT=2
    GROUP=3
    END_GROUP=4
    BANNER=5
    SEPARATOR=6
    COMMENT=7
    BLANK=8
    LINE=9

    def __init__(self, input:TokenStream, output:TextIO = sys.stdout):
        super().__init__(input, output)
        self.checkVersion("4.13.2")
        self._interp = ParserATNSimulator(self, self.atn, self.decisionsToDFA, self.sharedContextCache)
        self._predicates = None




    class ConfigurationContext(CiscoContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def EOF(self):
            return self.getToken(CiscoParser.EOF, 0)

        def node(self, i:int=None):
            if i is None:
                return self.getTypedRuleContexts(CiscoParser.NodeContext)
            else:
                return self.getTypedRuleContext(CiscoParser.NodeContext,i)


        def getRuleIndex(self):
            return CiscoParser.RULE_configuration

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterConfiguration" ):
                listener.enterConfiguration(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitConfiguration" ):
                listener.exitConfiguration(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitConfiguration" ):
                return visitor.visitConfiguration(self)
            else:
                return visitor.visitChildren(self)




    def configuration(self):

        localctx = CiscoParser.ConfigurationContext(self, self._ctx, self.state)
        self.enterRule(localctx, 0, self.RULE_configuration)
        self._la = 0 # Token type
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 9
            self._errHandler.sync(self)
            _la = self._input.LA(1)
            while (((_la) & ~0x3f) == 0 and ((1 << _la) & 1000) != 0):
                self.state = 6
                self.node()
                self.state = 11
                self._errHandler.sync(self)
                _la = self._input.LA(1)

            self.state = 12
            self.match(CiscoParser.EOF)
        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class NodeContext(CiscoContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def GROUP(self):
            return self.getToken(CiscoParser.GROUP, 0)

        def END_GROUP(self):
            return self.getToken(CiscoParser.END_GROUP, 0)

        def trivia(self, i:int=None):
            if i is None:
                return self.getTypedRuleContexts(CiscoParser.TriviaContext)
            else:
                return self.getTypedRuleContext(CiscoParser.TriviaContext,i)


        def INDENT(self):
            return self.getToken(CiscoParser.INDENT, 0)

        def DEDENT(self):
            return self.getToken(CiscoParser.DEDENT, 0)

        def node(self, i:int=None):
            if i is None:
                return self.getTypedRuleContexts(CiscoParser.NodeContext)
            else:
                return self.getTypedRuleContext(CiscoParser.NodeContext,i)


        def LINE(self):
            return self.getToken(CiscoParser.LINE, 0)

        def BANNER(self):
            return self.getToken(CiscoParser.BANNER, 0)

        def SEPARATOR(self):
            return self.getToken(CiscoParser.SEPARATOR, 0)

        def COMMENT(self):
            return self.getToken(CiscoParser.COMMENT, 0)

        def BLANK(self):
            return self.getToken(CiscoParser.BLANK, 0)

        def getRuleIndex(self):
            return CiscoParser.RULE_node

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterNode" ):
                listener.enterNode(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitNode" ):
                listener.exitNode(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitNode" ):
                return visitor.visitNode(self)
            else:
                return visitor.visitChildren(self)




    def node(self):

        localctx = CiscoParser.NodeContext(self, self._ctx, self.state)
        self.enterRule(localctx, 2, self.RULE_node)
        self._la = 0 # Token type
        try:
            self.state = 53
            self._errHandler.sync(self)
            token = self._input.LA(1)
            if token in [3]:
                self.enterOuterAlt(localctx, 1)
                self.state = 14
                self.match(CiscoParser.GROUP)
                self.state = 18
                self._errHandler.sync(self)
                _la = self._input.LA(1)
                while _la==7 or _la==8:
                    self.state = 15
                    self.trivia()
                    self.state = 20
                    self._errHandler.sync(self)
                    _la = self._input.LA(1)

                self.state = 29
                self._errHandler.sync(self)
                _la = self._input.LA(1)
                if _la==1:
                    self.state = 21
                    self.match(CiscoParser.INDENT)
                    self.state = 25
                    self._errHandler.sync(self)
                    _la = self._input.LA(1)
                    while (((_la) & ~0x3f) == 0 and ((1 << _la) & 1000) != 0):
                        self.state = 22
                        self.node()
                        self.state = 27
                        self._errHandler.sync(self)
                        _la = self._input.LA(1)

                    self.state = 28
                    self.match(CiscoParser.DEDENT)


                self.state = 31
                self.match(CiscoParser.END_GROUP)
                pass
            elif token in [9]:
                self.enterOuterAlt(localctx, 2)
                self.state = 32
                self.match(CiscoParser.LINE)
                self.state = 36
                self._errHandler.sync(self)
                _alt = self._interp.adaptivePredict(self._input,4,self._ctx)
                while _alt!=2 and _alt!=ATN.INVALID_ALT_NUMBER:
                    if _alt==1:
                        self.state = 33
                        self.trivia() 
                    self.state = 38
                    self._errHandler.sync(self)
                    _alt = self._interp.adaptivePredict(self._input,4,self._ctx)

                self.state = 47
                self._errHandler.sync(self)
                _la = self._input.LA(1)
                if _la==1:
                    self.state = 39
                    self.match(CiscoParser.INDENT)
                    self.state = 41 
                    self._errHandler.sync(self)
                    _la = self._input.LA(1)
                    while True:
                        self.state = 40
                        self.node()
                        self.state = 43 
                        self._errHandler.sync(self)
                        _la = self._input.LA(1)
                        if not ((((_la) & ~0x3f) == 0 and ((1 << _la) & 1000) != 0)):
                            break

                    self.state = 45
                    self.match(CiscoParser.DEDENT)


                pass
            elif token in [5]:
                self.enterOuterAlt(localctx, 3)
                self.state = 49
                self.match(CiscoParser.BANNER)
                pass
            elif token in [6]:
                self.enterOuterAlt(localctx, 4)
                self.state = 50
                self.match(CiscoParser.SEPARATOR)
                pass
            elif token in [7]:
                self.enterOuterAlt(localctx, 5)
                self.state = 51
                self.match(CiscoParser.COMMENT)
                pass
            elif token in [8]:
                self.enterOuterAlt(localctx, 6)
                self.state = 52
                self.match(CiscoParser.BLANK)
                pass
            else:
                raise NoViableAltException(self)

        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class TriviaContext(CiscoContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def COMMENT(self):
            return self.getToken(CiscoParser.COMMENT, 0)

        def BLANK(self):
            return self.getToken(CiscoParser.BLANK, 0)

        def getRuleIndex(self):
            return CiscoParser.RULE_trivia

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterTrivia" ):
                listener.enterTrivia(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitTrivia" ):
                listener.exitTrivia(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitTrivia" ):
                return visitor.visitTrivia(self)
            else:
                return visitor.visitChildren(self)




    def trivia(self):

        localctx = CiscoParser.TriviaContext(self, self._ctx, self.state)
        self.enterRule(localctx, 4, self.RULE_trivia)
        self._la = 0 # Token type
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 55
            _la = self._input.LA(1)
            if not(_la==7 or _la==8):
                self._errHandler.recoverInline(self)
            else:
                self._errHandler.reportMatch(self)
                self.consume()
        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx





