"""English messages whose arguments can be rendered later in the desktop language."""


class Message(str):
    def __new__(cls, template, **params):
        value = template.format(**params) if params else template
        result = super().__new__(cls, value)
        result.template = template
        result.params = params
        return result


def message(template, **params):
    return Message(template, **params)


def error_detail(exc):
    if len(exc.args) == 1 and isinstance(exc.args[0], Message):
        return exc.args[0]
    return message(str(exc))
