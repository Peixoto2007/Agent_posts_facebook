from google.adk.agents.llm_agent import Agent

root_agent = Agent(
    model='gemini-3.5-flash',
    name='especialist_migration_course',
    description='Você e um agente especialista em transferencia de curso de faculdades do brasil para o Chile, que ajuda os brasileiros a transferir para universidades do chile',
    instruction='Você e um agente especialista em transferencia de curso de faculdades. Ajude os e  studantes a migrarem a outras faculdades do chile',
)
