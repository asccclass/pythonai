"""Safe, structured explanations for cleanup blocked by background activity."""


class WorkerBlocker(str):
    def __new__(cls, label, detail):
        value = super().__new__(cls, label)
        value.detail = detail
        return value


class CleanupBlocked(ValueError):
    def __init__(self, blockers):
        self.blockers = [getattr(item, 'detail', {
            'kind':str(item).split(':',1)[0], 'id':str(item).split(':',1)[-1],
            'status':'active', 'retryable':True,
        }) for item in blockers]
        self.retryable = bool(self.blockers) and all(item['retryable'] for item in self.blockers)
        descriptions = []
        for item in self.blockers:
            if item['kind']=='memory-worker':
                descriptions.append(
                    f"記憶工作 #{item['job_id']}（{item['job_type']}，狀態 {item['status']}，"
                    f"擁有者 {item['owner_state']}，租約{'已到期' if item['lease_expired'] else '有效'}）")
            else:
                descriptions.append(f"{item['kind']} {item.get('id','')}（{item['status']}）")
        instruction = ('背景工作仍在執行，請等待結束後重試。' if self.retryable else
                       '需要確認工作程序已停止並備份資料庫，再執行恢復；目前保留檔案。')
        super().__init__('刪除受阻：'+'；'.join(descriptions)+'。'+instruction)
