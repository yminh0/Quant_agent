import ast  # AST 모듈

class CodeValidator:
    # 허용된 라이브러리 목록, 라이브러리는 이 범위 내에서만 사용 가능
    ALLOWED_MODULES = {'pandas', 'numpy', 'backtrader', 'datetime', 'math'}
    # 사용 금지된 이름 목록 (모듈, 함수 등)
    FORBIDDEN_NAMES = {'eval', 'exec', 'open', '__import__', 'getattr', 'setattr'}

    def validate(self, code: str):
        try:
            # 코드를 트리 구조로 변환
            tree = ast.parse(code)
            #트리의 모든 노드를 하나씩 탐색
            for node in ast.walk(tree):
                # Import문 검사
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    # 모듈 이름 추출
                    module_name = node.names[0].name if isinstance(node, ast.Import) else node.module
                    # 허용 목록에 있는지 확인
                    if module_name.split('.')[0] not in self.ALLOWED_MODULES:
                        return False, f"Unauthorized module: {module_name}"

                # 함수명/변수명 검사
                if hasattr(node, 'id') and node.id in self.FORBIDDEN_NAMES:
                    return False, f"Forbidden name: {node.id}"
                
                # 속성명/메서드명 검사
                if hasattr(node, 'attr') and node.attr in self.FORBIDDEN_NAMES:
                    return False, f"Forbidden attribute: {node.attr}"
                
            # 모든 검사 통과
            return True, "Valid"
        
        except Exception as e:
            # 문법 오류인 경우
            return False, str(e)