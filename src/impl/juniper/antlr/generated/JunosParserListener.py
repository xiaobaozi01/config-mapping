# Generated from JunosParser.g4 by ANTLR 4.13.2
from antlr4 import *
if "." in __name__:
    from .JunosParser import JunosParser
else:
    from JunosParser import JunosParser

from ..context import JunosContext


# This class defines a complete listener for a parse tree produced by JunosParser.
class JunosParserListener(ParseTreeListener):

    # Enter a parse tree produced by JunosParser#configuration.
    def enterConfiguration(self, ctx:JunosParser.ConfigurationContext):
        pass

    # Exit a parse tree produced by JunosParser#configuration.
    def exitConfiguration(self, ctx:JunosParser.ConfigurationContext):
        pass


    # Enter a parse tree produced by JunosParser#element.
    def enterElement(self, ctx:JunosParser.ElementContext):
        pass

    # Exit a parse tree produced by JunosParser#element.
    def exitElement(self, ctx:JunosParser.ElementContext):
        pass


    # Enter a parse tree produced by JunosParser#statement.
    def enterStatement(self, ctx:JunosParser.StatementContext):
        pass

    # Exit a parse tree produced by JunosParser#statement.
    def exitStatement(self, ctx:JunosParser.StatementContext):
        pass


    # Enter a parse tree produced by JunosParser#command.
    def enterCommand(self, ctx:JunosParser.CommandContext):
        pass

    # Exit a parse tree produced by JunosParser#command.
    def exitCommand(self, ctx:JunosParser.CommandContext):
        pass


    # Enter a parse tree produced by JunosParser#commandPart.
    def enterCommandPart(self, ctx:JunosParser.CommandPartContext):
        pass

    # Exit a parse tree produced by JunosParser#commandPart.
    def exitCommandPart(self, ctx:JunosParser.CommandPartContext):
        pass



del JunosParser