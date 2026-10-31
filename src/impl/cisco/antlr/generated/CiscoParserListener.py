# Generated from CiscoParser.g4 by ANTLR 4.13.2
from antlr4 import *
if "." in __name__:
    from .CiscoParser import CiscoParser
else:
    from CiscoParser import CiscoParser

from ..context import CiscoContext


# This class defines a complete listener for a parse tree produced by CiscoParser.
class CiscoParserListener(ParseTreeListener):

    # Enter a parse tree produced by CiscoParser#configuration.
    def enterConfiguration(self, ctx:CiscoParser.ConfigurationContext):
        pass

    # Exit a parse tree produced by CiscoParser#configuration.
    def exitConfiguration(self, ctx:CiscoParser.ConfigurationContext):
        pass


    # Enter a parse tree produced by CiscoParser#node.
    def enterNode(self, ctx:CiscoParser.NodeContext):
        pass

    # Exit a parse tree produced by CiscoParser#node.
    def exitNode(self, ctx:CiscoParser.NodeContext):
        pass


    # Enter a parse tree produced by CiscoParser#trivia.
    def enterTrivia(self, ctx:CiscoParser.TriviaContext):
        pass

    # Exit a parse tree produced by CiscoParser#trivia.
    def exitTrivia(self, ctx:CiscoParser.TriviaContext):
        pass



del CiscoParser