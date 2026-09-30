import unittest

import memory


class DurableSignalGateTests(unittest.TestCase):
    def test_reported_misses_now_fire(self):
        # Turnos reais que o gate antigo (regex) deixou passar e foram parar no cache.
        for msg in (
            "quero que a partir de agora voce me chame de Bruno ao inves de Ana para toda as interações",
            "Nunca me ofereça nada acima de R$ 800, é o meu limite.",
            "como é para voce me chamar a partir de agora?",
            "Não me mande e-mail, só WhatsApp",
            "PODE ME CHAMAR de Dri",
        ):
            self.assertTrue(memory.should_extract(msg), msg)

    def test_recall_questions_fire(self):
        # Perguntas SOBRE a memória também dependem dela — nunca vão ao cache.
        for msg in ("O que você sabe sobre mim?", "Você lembra de mim?",
                    "qual é o meu apelido?", "quais são minhas preferências?"):
            self.assertTrue(memory.should_extract(msg), msg)

    def test_original_signals_still_fire(self):
        for msg in ("prefiro e-mail", "Meu nome é Marina", "sou alérgica a látex",
                    "moro em Curitiba", "me avise quando chegar"):
            self.assertTrue(memory.should_extract(msg), msg)

    def test_transactional_and_generic_do_not_fire(self):
        for msg in ("Como voce pode me ajudar?", "qual o status do PED-1005?",
                    "como peço reembolso?", "quem é o presidente da colombia?"):
            self.assertFalse(memory.should_extract(msg), msg)

    def test_no_substring_false_positive(self):
        # "costumo" dentro de outra palavra / "aprefiro" não devem disparar
        self.assertFalse(memory.should_extract("acostumovel produto"))


if __name__ == "__main__":
    unittest.main()
