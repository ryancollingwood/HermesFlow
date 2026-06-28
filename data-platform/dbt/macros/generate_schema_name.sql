{# dbt-core's default macro concatenates as <default_schema>_<custom_schema>
   (e.g. "stg_data_platform") instead of using the configured schema as-is.
   We want models to land exactly in the schema they request — staging in
   `stg`, marts in `data_platform` (the latter via the Postgres attach) —
   so this override returns the custom schema unprefixed. #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
