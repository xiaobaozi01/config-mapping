# Generated from JunosParser.g4 by ANTLR 4.13.2
from antlr4 import *
if "." in __name__:
    from .JunosParser import JunosParser
else:
    from JunosParser import JunosParser

from ..context import JunosContext


# This class defines a complete generic visitor for a parse tree produced by JunosParser.

class JunosParserVisitor(ParseTreeVisitor):

    # Visit a parse tree produced by JunosParser#configuration.
    def visitConfiguration(self, ctx:JunosParser.ConfigurationContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by JunosParser#element.
    def visitElement(self, ctx:JunosParser.ElementContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by JunosParser#statement.
    def visitStatement(self, ctx:JunosParser.StatementContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by JunosParser#command.
    def visitCommand(self, ctx:JunosParser.CommandContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by JunosParser#commandPart.
    def visitCommandPart(self, ctx:JunosParser.CommandPartContext):
        return self.visitChildren(ctx)



del JunosParser