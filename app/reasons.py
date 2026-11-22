REASONS = {
    'COLUMN_COUNT':'列数错误', 'MISSING_ID':'编号缺失', 'DUPLICATE_CONFLICT':'重复编号冲突',
    'MISSING_VALUE':'数值缺失', 'BAD_NUMBER':'数值格式错误', 'BAD_TIME':'时间格式错误',
    'BAD_UNIT':'单位不受支持', 'UNKNOWN_REFERENCE':'引用对象不存在', 'INVALID_RANGE':'范围错误',
    'VERSION_CONFLICT':'版本冲突', 'EMPTY_FILE':'空文件', 'BLANK_FILE':'全空行',
    'HEADER_ONLY':'仅有表头', 'STALE_VERSION':'复核版本已更新', 'NOT_FOUND':'批次不存在',
    'MANUAL_HOLD':'争议登记未闭合', 'DISPUTE_QUARANTINE':'隔离证据关联未闭合',
    'CORRECTION_OPEN':'更正链未闭合', 'EVIDENCE_SHARED':'证据被待处理票引用',
    'EVIDENCE_MISMATCH':'票据与封存证据对不上', 'SEAL_MISMATCH':'封存校验失败',
    'NOT_EXPIRED':'保留期未届满', 'DUPLICATE_DISPOSAL':'相同处置结果已存在',
}
def reject(state, record, code, detail):
    state['quarantine'].append({'line':record.get('_line',0),'id':record.get('id',''),
       'code':code,'detail':detail,'raw':{k:v for k,v in record.items() if not k.startswith('_')}})
