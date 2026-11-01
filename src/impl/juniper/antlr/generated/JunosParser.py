# Generated from JunosParser.g4 by ANTLR 4.13.2
# encoding: utf-8
from antlr4 import *
from io import StringIO
import sys
if sys.version_info[1] > 5:
	from typing import TextIO
else:
	from typing.io import TextIO


from ..context import JunosContext

def serializedATN():
    return [
        4,1,11,50,2,0,7,0,2,1,7,1,2,2,7,2,2,3,7,3,2,4,7,4,1,0,5,0,12,8,0,
        10,0,12,0,15,9,0,1,0,1,0,1,1,1,1,1,1,3,1,22,8,1,1,2,1,2,1,2,1,2,
        5,2,28,8,2,10,2,12,2,31,9,2,1,2,1,2,3,2,35,8,2,3,2,37,8,2,1,2,1,
        2,3,2,41,8,2,1,3,4,3,44,8,3,11,3,12,3,45,1,4,1,4,1,4,0,0,5,0,2,4,
        6,8,0,1,2,0,5,8,10,11,52,0,13,1,0,0,0,2,21,1,0,0,0,4,23,1,0,0,0,
        6,43,1,0,0,0,8,47,1,0,0,0,10,12,3,2,1,0,11,10,1,0,0,0,12,15,1,0,
        0,0,13,11,1,0,0,0,13,14,1,0,0,0,14,16,1,0,0,0,15,13,1,0,0,0,16,17,
        5,0,0,1,17,1,1,0,0,0,18,22,3,4,2,0,19,22,5,4,0,0,20,22,5,5,0,0,21,
        18,1,0,0,0,21,19,1,0,0,0,21,20,1,0,0,0,22,3,1,0,0,0,23,36,3,6,3,
        0,24,37,5,3,0,0,25,29,5,1,0,0,26,28,3,2,1,0,27,26,1,0,0,0,28,31,
        1,0,0,0,29,27,1,0,0,0,29,30,1,0,0,0,30,32,1,0,0,0,31,29,1,0,0,0,
        32,34,5,2,0,0,33,35,5,3,0,0,34,33,1,0,0,0,34,35,1,0,0,0,35,37,1,
        0,0,0,36,24,1,0,0,0,36,25,1,0,0,0,37,40,1,0,0,0,38,39,4,2,0,0,39,
        41,5,4,0,0,40,38,1,0,0,0,40,41,1,0,0,0,41,5,1,0,0,0,42,44,3,8,4,
        0,43,42,1,0,0,0,44,45,1,0,0,0,45,43,1,0,0,0,45,46,1,0,0,0,46,7,1,
        0,0,0,47,48,7,0,0,0,48,9,1,0,0,0,7,13,21,29,34,36,40,45
    ]

class JunosParser ( Parser ):

    grammarFileName = "JunosParser.g4"

    atn = ATNDeserializer().deserialize(serializedATN())

    decisionsToDFA = [ DFA(ds, i) for i, ds in enumerate(atn.decisionToState) ]

    sharedContextCache = PredictionContextCache()

    literalNames = [ "<INVALID>", "'{'", "'}'", "';'", "<INVALID>", "<INVALID>", 
                     "<INVALID>", "<INVALID>", "<INVALID>", "<INVALID>", 
                     "'/'" ]

    symbolicNames = [ "<INVALID>", "LBRACE", "RBRACE", "SEMI", "LINE_COMMENT", 
                      "BLOCK_COMMENT", "DOUBLE_QUOTED_STRING", "SINGLE_QUOTED_STRING", 
                      "TEXT", "WHITESPACE", "SLASH", "OTHER" ]

    RULE_configuration = 0
    RULE_element = 1
    RULE_statement = 2
    RULE_command = 3
    RULE_commandPart = 4

    ruleNames =  [ "configuration", "element", "statement", "command", "commandPart" ]

    EOF = Token.EOF
    LBRACE=1
    RBRACE=2
    SEMI=3
    LINE_COMMENT=4
    BLOCK_COMMENT=5
    DOUBLE_QUOTED_STRING=6
    SINGLE_QUOTED_STRING=7
    TEXT=8
    WHITESPACE=9
    SLASH=10
    OTHER=11

    def __init__(self, input:TokenStream, output:TextIO = sys.stdout):
        super().__init__(input, output)
        self.checkVersion("4.13.2")
        self._interp = ParserATNSimulator(self, self.atn, self.decisionsToDFA, self.sharedContextCache)
        self._predicates = None




    class ConfigurationContext(JunosContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def EOF(self):
            return self.getToken(JunosParser.EOF, 0)

        def element(self, i:int=None):
            if i is None:
                return self.getTypedRuleContexts(JunosParser.ElementContext)
            else:
                return self.getTypedRuleContext(JunosParser.ElementContext,i)


        def getRuleIndex(self):
            return JunosParser.RULE_configuration

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

        localctx = JunosParser.ConfigurationContext(self, self._ctx, self.state)
        self.enterRule(localctx, 0, self.RULE_configuration)
        self._la = 0 # Token type
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 13
            self._errHandler.sync(self)
            _la = self._input.LA(1)
            while (((_la) & ~0x3f) == 0 and ((1 << _la) & 3568) != 0):
                self.state = 10
                self.element()
                self.state = 15
                self._errHandler.sync(self)
                _la = self._input.LA(1)

            self.state = 16
            self.match(JunosParser.EOF)
        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class ElementContext(JunosContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def statement(self):
            return self.getTypedRuleContext(JunosParser.StatementContext,0)


        def LINE_COMMENT(self):
            return self.getToken(JunosParser.LINE_COMMENT, 0)

        def BLOCK_COMMENT(self):
            return self.getToken(JunosParser.BLOCK_COMMENT, 0)

        def getRuleIndex(self):
            return JunosParser.RULE_element

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterElement" ):
                listener.enterElement(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitElement" ):
                listener.exitElement(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitElement" ):
                return visitor.visitElement(self)
            else:
                return visitor.visitChildren(self)




    def element(self):

        localctx = JunosParser.ElementContext(self, self._ctx, self.state)
        self.enterRule(localctx, 2, self.RULE_element)
        try:
            self.state = 21
            self._errHandler.sync(self)
            la_ = self._interp.adaptivePredict(self._input,1,self._ctx)
            if la_ == 1:
                self.enterOuterAlt(localctx, 1)
                self.state = 18
                self.statement()
                pass

            elif la_ == 2:
                self.enterOuterAlt(localctx, 2)
                self.state = 19
                self.match(JunosParser.LINE_COMMENT)
                pass

            elif la_ == 3:
                self.enterOuterAlt(localctx, 3)
                self.state = 20
                self.match(JunosParser.BLOCK_COMMENT)
                pass


        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class StatementContext(JunosContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def command(self):
            return self.getTypedRuleContext(JunosParser.CommandContext,0)


        def SEMI(self):
            return self.getToken(JunosParser.SEMI, 0)

        def LBRACE(self):
            return self.getToken(JunosParser.LBRACE, 0)

        def RBRACE(self):
            return self.getToken(JunosParser.RBRACE, 0)

        def LINE_COMMENT(self):
            return self.getToken(JunosParser.LINE_COMMENT, 0)

        def element(self, i:int=None):
            if i is None:
                return self.getTypedRuleContexts(JunosParser.ElementContext)
            else:
                return self.getTypedRuleContext(JunosParser.ElementContext,i)


        def getRuleIndex(self):
            return JunosParser.RULE_statement

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterStatement" ):
                listener.enterStatement(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitStatement" ):
                listener.exitStatement(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitStatement" ):
                return visitor.visitStatement(self)
            else:
                return visitor.visitChildren(self)




    def statement(self):

        localctx = JunosParser.StatementContext(self, self._ctx, self.state)
        self.enterRule(localctx, 4, self.RULE_statement)
        self._la = 0 # Token type
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 23
            self.command()
            self.state = 36
            self._errHandler.sync(self)
            token = self._input.LA(1)
            if token in [3]:
                self.state = 24
                self.match(JunosParser.SEMI)
                pass
            elif token in [1]:
                self.state = 25
                self.match(JunosParser.LBRACE)
                self.state = 29
                self._errHandler.sync(self)
                _la = self._input.LA(1)
                while (((_la) & ~0x3f) == 0 and ((1 << _la) & 3568) != 0):
                    self.state = 26
                    self.element()
                    self.state = 31
                    self._errHandler.sync(self)
                    _la = self._input.LA(1)

                self.state = 32
                self.match(JunosParser.RBRACE)
                self.state = 34
                self._errHandler.sync(self)
                la_ = self._interp.adaptivePredict(self._input,3,self._ctx)
                if la_ == 1:
                    self.state = 33
                    self.match(JunosParser.SEMI)


                pass
            else:
                raise NoViableAltException(self)

            self.state = 40
            self._errHandler.sync(self)
            la_ = self._interp.adaptivePredict(self._input,5,self._ctx)
            if la_ == 1:
                self.state = 38
                if not self._input.LT(-1).line == self._input.LT(1).line:
                    from antlr4.error.Errors import FailedPredicateException
                    raise FailedPredicateException(self, "self._input.LT(-1).line == self._input.LT(1).line")
                self.state = 39
                self.match(JunosParser.LINE_COMMENT)


        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class CommandContext(JunosContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def commandPart(self, i:int=None):
            if i is None:
                return self.getTypedRuleContexts(JunosParser.CommandPartContext)
            else:
                return self.getTypedRuleContext(JunosParser.CommandPartContext,i)


        def getRuleIndex(self):
            return JunosParser.RULE_command

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterCommand" ):
                listener.enterCommand(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitCommand" ):
                listener.exitCommand(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitCommand" ):
                return visitor.visitCommand(self)
            else:
                return visitor.visitChildren(self)




    def command(self):

        localctx = JunosParser.CommandContext(self, self._ctx, self.state)
        self.enterRule(localctx, 6, self.RULE_command)
        self._la = 0 # Token type
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 43 
            self._errHandler.sync(self)
            _la = self._input.LA(1)
            while True:
                self.state = 42
                self.commandPart()
                self.state = 45 
                self._errHandler.sync(self)
                _la = self._input.LA(1)
                if not ((((_la) & ~0x3f) == 0 and ((1 << _la) & 3552) != 0)):
                    break

        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class CommandPartContext(JunosContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def TEXT(self):
            return self.getToken(JunosParser.TEXT, 0)

        def DOUBLE_QUOTED_STRING(self):
            return self.getToken(JunosParser.DOUBLE_QUOTED_STRING, 0)

        def SINGLE_QUOTED_STRING(self):
            return self.getToken(JunosParser.SINGLE_QUOTED_STRING, 0)

        def BLOCK_COMMENT(self):
            return self.getToken(JunosParser.BLOCK_COMMENT, 0)

        def SLASH(self):
            return self.getToken(JunosParser.SLASH, 0)

        def OTHER(self):
            return self.getToken(JunosParser.OTHER, 0)

        def getRuleIndex(self):
            return JunosParser.RULE_commandPart

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterCommandPart" ):
                listener.enterCommandPart(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitCommandPart" ):
                listener.exitCommandPart(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitCommandPart" ):
                return visitor.visitCommandPart(self)
            else:
                return visitor.visitChildren(self)




    def commandPart(self):

        localctx = JunosParser.CommandPartContext(self, self._ctx, self.state)
        self.enterRule(localctx, 8, self.RULE_commandPart)
        self._la = 0 # Token type
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 47
            _la = self._input.LA(1)
            if not((((_la) & ~0x3f) == 0 and ((1 << _la) & 3552) != 0)):
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



    def sempred(self, localctx:RuleContext, ruleIndex:int, predIndex:int):
        if self._predicates == None:
            self._predicates = dict()
        self._predicates[2] = self.statement_sempred
        pred = self._predicates.get(ruleIndex, None)
        if pred is None:
            raise Exception("No predicate with index:" + str(ruleIndex))
        else:
            return pred(localctx, predIndex)

    def statement_sempred(self, localctx:StatementContext, predIndex:int):
            if predIndex == 0:
                return self._input.LT(-1).line == self._input.LT(1).line
         




