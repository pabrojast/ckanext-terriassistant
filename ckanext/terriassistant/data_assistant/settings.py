from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    model: str = "deepseek-flash"
    ttl: int = 1800
    timeout: int = 300
    max_rounds: int = 8
    max_calls: int = 16
    max_bytes: int = 25 * 1024 * 1024
    max_rows: int = 150000
    request_bytes: int = 4 * 1024 * 1024
    max_result_rows: int = 200

    @classmethod
    def from_config(cls, config):
        prefix = "ckanext.terriassistant.data_"
        values = {}
        for key, field in cls.__dataclass_fields__.items():
            value = config.get(prefix + key, field.default)
            if key == "enabled":
                value = str(value).lower() in ("true", "1", "yes")
            elif key != "model":
                value = max(1, int(value))
            values[key] = value
        return cls(**values)
