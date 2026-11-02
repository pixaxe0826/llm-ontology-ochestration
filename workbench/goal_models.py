"""Goal Builder catalog; independent from Work/Chat provider settings."""
DEFAULT_BUILDER_MODEL = 'muse-spark-1.3'
BUILDER_MODELS = ('muse-spark-1.3','muse-spark-1.3-contributor','muse-spark-1.2','muse-spark-1.2-contributor','muse-spark-1.1')


def validate_builder_model(value):
    if not isinstance(value,str) or value not in BUILDER_MODELS:
        raise ValueError('작업 모드에서 사용할 Builder 모델을 선택하세요.')
    return value


def builder_catalog(available):
    if not isinstance(available,list):raise ValueError('모델 목록을 불러오지 못함')
    return {'models':[model for model in BUILDER_MODELS if model in available], 'default':DEFAULT_BUILDER_MODEL}
