# Self-Healing para workloads no Amazon EKS

Este projeto implementa uma função AWS Lambda que recebe alertas do Grafana e solicita um reinício gradual de um Deployment no Kubernetes. A integração foi pensada para recuperar workloads com falhas sem conceder à função permissões administrativas sobre o cluster.

## Arquitetura

```text
Grafana
	│ Webhook HTTPS (POST + Bearer token)
	▼
Amazon API Gateway (HTTP API)
	│ Invocação da função
	▼
AWS Lambda (app.handler)
	├─ autentica o webhook e valida o alerta
	├─ assina uma solicitação STS com as credenciais IAM da função
	└─ envia um PATCH autenticado à API do Kubernetes no EKS
			 └─ altera a anotação restartedAt do pod template
						└─ Kubernetes recria os pods segundo a estratégia de rollout
```

O código da aplicação está em [`app.py`](app.py). O API Gateway expõe o endpoint HTTPS para o Grafana, e a Lambda faz a ponte entre o alerta e a API Kubernetes do cluster. O manifesto [`rbac.yaml`](rbac.yaml) define o acesso Kubernetes limitado à operação de patch de Deployments no namespace configurado.

## Fluxo de recuperação

1. A Lambda aceita requisições `POST` e valida o cabeçalho `Authorization: Bearer ...` usando comparação em tempo constante.
2. O corpo JSON é interpretado, inclusive quando o API Gateway o entrega codificado em Base64.
3. Alertas resolvidos ou sem alertas em estado `firing` são ignorados sem alterar o cluster.
4. Para um alerta em disparo, a Lambda exige os labels `namespace` e `deployment`. O namespace precisa corresponder exatamente a `ALLOWED_NAMESPACE`; o nome do Deployment precisa obedecer ao formato DNS aceito pelo Kubernetes.
5. A Lambda cria um token de autenticação EKS temporário assinando uma solicitação `GetCallerIdentity` do STS com as credenciais IAM disponíveis no runtime.
6. A Lambda verifica o certificado TLS da API do cluster e envia um `PATCH` com a anotação `kubectl.kubernetes.io/restartedAt` no pod template.
7. A alteração do template faz o Kubernetes iniciar um rolling update, respeitando a estratégia de atualização configurada no Deployment. A resposta confirma que o pedido foi enviado; ela não espera o rollout terminar.

Se um webhook contiver mais de um alerta em disparo, somente o primeiro é usado como alvo. Configure o agrupamento/notificação do Grafana de acordo com esse comportamento.

## Decisões de implementação

- **Reinício nativo do Kubernetes:** alterar `kubectl.kubernetes.io/restartedAt` no pod template equivale, na prática, a solicitar um `kubectl rollout restart`. Não é necessário excluir pods diretamente nem implementar uma estratégia própria de rollout.
- **Privilégio mínimo:** há duas barreiras para limitar o alvo: a Lambda compara o namespace com `ALLOWED_NAMESPACE`, e o RBAC restringe o grupo da função aos recursos e namespace autorizados. O manifesto concede `patch` e `get` em Deployments; `get` é útil para diagnóstico, embora o reinício precise de `patch`.
- **Autenticação AWS nativa:** o token EKS é derivado das credenciais IAM da função e assinado com SigV4. Não é armazenada uma credencial Kubernetes de longa duração.
- **TLS validado:** o certificado CA fornecido pelo EKS é usado para validar a conexão HTTPS com a API do cluster.
- **Dependências enxutas:** a Lambda usa a biblioteca padrão do Python e o `boto3`/`botocore` já fornecido pelo runtime AWS. `requirements.txt` é destinado ao desenvolvimento e aos testes locais.
- **Falha fechada:** sem `WEBHOOK_BEARER_TOKEN` ou `ALLOWED_NAMESPACE`, a função não processa alertas. Nomes de Deployment são validados antes de serem inseridos no caminho da API Kubernetes.
- **Resposta idempotente para alvo ausente:** um `404` do Kubernetes é tratado como Deployment que já não existe, e retorna sucesso sem ação adicional.

## Pré-requisitos

- Python 3.10 ou superior para desenvolvimento local; o guia de provisionamento usa Python 3.12 no runtime da Lambda.
- Uma conta AWS com permissões para configurar IAM, Lambda, API Gateway e EKS.
- Um cluster EKS com o endpoint Kubernetes acessível pela Lambda.
- Grafana Alerting configurado para enviar um webhook com os labels necessários.
- `kubectl` configurado para aplicar RBAC e verificar o Deployment.

## Configuração

A Lambda lê estas variáveis de ambiente:

| Variável | Obrigatória | Finalidade |
| --- | --- | --- |
| `WEBHOOK_BEARER_TOKEN` | Sim | Segredo compartilhado validado no cabeçalho `Authorization` do webhook. |
| `ALLOWED_NAMESPACE` | Sim | Único namespace no qual um Deployment pode ser reiniciado. |
| `EKS_CLUSTER_NAME` | Sim | Nome do cluster usado na assinatura do token de autenticação EKS. |
| `EKS_CLUSTER_ENDPOINT` | Sim | Endpoint HTTPS da API Kubernetes do cluster. |
| `EKS_CLUSTER_CA_CERTIFICATE_BASE64` | Sim | Certificado CA do cluster codificado em Base64. |
| `AWS_REGION` | Sim | Região AWS; fornecida automaticamente pelo runtime Lambda. |

Além das variáveis, a função precisa de uma role IAM executável pelo serviço Lambda e autorizada a chamar `sts:GetCallerIdentity`. O principal dessa role também precisa estar mapeado no EKS (por Access Entry ou `aws-auth`) para o grupo `self-healing`, ao qual o RoleBinding do [`rbac.yaml`](rbac.yaml) concede acesso. Ajuste o namespace do manifesto para que coincida com `ALLOWED_NAMESPACE`.

O endpoint do EKS deve ser alcançável pela Lambda na porta 443. Para um endpoint privado, configure a Lambda na VPC e nas sub-redes/rotas que alcançam o endpoint do cluster.

## Formato do webhook

O webhook deve enviar o token no cabeçalho `Authorization` e incluir `namespace` e `deployment` nos labels de cada alerta relevante:

```http
POST /
Authorization: Bearer <WEBHOOK_BEARER_TOKEN>
Content-Type: application/json
```

```json
{
	"status": "firing",
	"alerts": [
		{
			"status": "firing",
			"labels": {
				"alertname": "HighErrorRate",
				"namespace": "toggle-prod",
				"deployment": "auth-service"
			}
		}
	]
}
```

O valor de `namespace` no exemplo é ilustrativo: deve coincidir com `ALLOWED_NAMESPACE` e com o namespace definido no RBAC.

## Desenvolvimento e testes locais

No PowerShell, a partir da pasta do projeto:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Execute a suíte automatizada:

```powershell
pytest -q
```

Execute os cenários de smoke test:

```powershell
python test_local.py
```

Por padrão, o smoke test substitui a chamada ao EKS por uma simulação e não precisa de recursos AWS. O modo `--live` usa a implementação real e pode reiniciar um Deployment: execute-o somente depois de configurar credenciais AWS e todas as variáveis de ambiente para um cluster de teste apropriado.

Os testes automatizados verificam parsing do webhook, autenticação, validação do alvo, criação do token EKS, conteúdo do PATCH e mapeamento de erros. As chamadas externas são simuladas; portanto, a suíte não substitui uma validação de conectividade e permissões em um ambiente AWS real.

## Implantação

O [`setup-guide.md`](setup-guide.md) contém o passo a passo para criar a role IAM, configurar o acesso EKS/RBAC, publicar a Lambda, criar o HTTP API Gateway, conectar o Grafana e verificar o rollout.

Para a função Lambda, o handler é `app.handler`. Como `boto3` e `botocore` são fornecidos pelo runtime, o pacote de implantação pode conter somente `app.py` (por exemplo, um ZIP com esse arquivo). Não inclua o ambiente virtual nem os arquivos de teste no pacote.

## Respostas principais

| Código HTTP | Situação |
| --- | --- |
| `200` | Reinício solicitado, alerta sem ação ou Deployment já inexistente (`404` da API Kubernetes). |
| `400` | Corpo inválido, namespace não permitido ou label/nome de Deployment inválido. |
| `401` | Token do webhook ausente ou incorreto. |
| `405` | Método diferente de `POST`. |
| `500` | Configuração ausente ou falha inesperada durante a recuperação. |
| `502` | Falha de autorização ou outra falha retornada pela API Kubernetes. |
