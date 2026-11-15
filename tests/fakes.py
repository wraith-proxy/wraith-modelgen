from modelgen.introspect import IntrospectedColumn, IntrospectionError


class FakeIntrospector:
    """For tests and offline development. Pre-populated with schemas."""

    def __init__(
        self, schemas: dict[tuple[str, str, str], list[IntrospectedColumn]]
    ):
        self._schemas = schemas

    def get_columns(
        self, project: str, dataset: str, table: str
    ) -> list[IntrospectedColumn]:
        key = (project, dataset, table)
        if key not in self._schemas:
            raise IntrospectionError(
                f"No fake schema registered for {project}.{dataset}.{table}. "
                f"Registered: {list(self._schemas.keys())}"
            )
        return self._schemas[key]
