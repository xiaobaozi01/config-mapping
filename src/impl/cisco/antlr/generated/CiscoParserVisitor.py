# Generated from CiscoParser.g4 by ANTLR 4.13.2
from antlr4 import *
if "." in __name__:
    from .CiscoParser import CiscoParser
else:
    from CiscoParser import CiscoParser

from ..context import CiscoContext


# This class defines a complete generic visitor for a parse tree produced by CiscoParser.

class CiscoParserVisitor(ParseTreeVisitor):

    # Visit a parse tree produced by CiscoParser#configuration.
    def visitConfiguration(self, ctx:CiscoParser.ConfigurationContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by CiscoParser#node.
    def visitNode(self, ctx:CiscoParser.NodeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by CiscoParser#trivia.
    def visitTrivia(self, ctx:CiscoParser.TriviaContext):
        return self.visitChildren(ctx)



del CiscoParser