"""测试通过公开重写入口构造配置状态；不直接修改 ANTLR Context。"""


def nodes(document):
    return document.root.nodes


def replace_header(document, context, header):
    transaction = document.rewrite()
    transaction.replace_header(context, header, "test setup")
    transaction.commit()


def delete_context(document, context):
    transaction = document.rewrite()
    transaction.delete(context, "test setup")
    transaction.commit()


def insert_before(document, context, text):
    transaction = document.rewrite()
    transaction.insert_before(context, text, "test setup")
    transaction.commit()


def append_child(document, context, text):
    transaction = document.rewrite()
    transaction.append_child(context, text, "test setup")
    transaction.commit()
