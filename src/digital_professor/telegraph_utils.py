# telegraph_utils.py
import asyncio
import logging
from typing import Optional
from telegraph import Telegraph

logger = logging.getLogger(__name__)

class TelegraphService:
    def __init__(self):
        self.telegraph = Telegraph()
        self._account_created = False
    
    async def _ensure_account(self):
        """Асинхронно создает аккаунт Telegraph, если еще не создан"""
        if not self._account_created:
            try:
                # Создаем аккаунт в Telegraph
                account = self.telegraph.create_account(
                    short_name='course_bot',
                    author_name='Learning Bot',
                    author_url='https://t.me/napominalkin_daily_bot'  
                )
                logger.info(f"Telegraph account created: {account}")
                self._account_created = True
            except Exception as e:
                logger.error(f"Failed to create Telegraph account: {e}")
                raise
    
    async def create_page(self, title: str, content: str) -> Optional[str]:
        """
        Асинхронно создает страницу в Telegraph и возвращает URL
        
        Args:
            title: Заголовок страницы
            content: HTML-контент страницы
            
        Returns:
            URL страницы или None в случае ошибки
        """
        try:
            # Убеждаемся, что аккаунт создан
            await self._ensure_account()
            
            # Создаем страницу
            # Telegraph ожидает HTML, поэтому оборачиваем контент в базовую структуру
            html_content = self._format_content(content)
            
            response = self.telegraph.create_page(
                title=title,
                html_content=html_content,
                author_name='Learning Bot'
            )
            
            url = response.get('url')
            if url:
                logger.info(f"Created Telegraph page: {url}")
                return url
            else:
                logger.error("Telegraph API returned no URL")
                return None
                
        except Exception as e:
            logger.error(f"Failed to create Telegraph page '{title}': {e}")
            return None
    
    def _format_content(self, content: str) -> str:
        """
        Форматирует контент для Telegraph (поддерживает ограниченный набор тегов)
        Разрешенные теги: a, aside, b, blockquote, br, code, em, figcaption, figure, h3, h4, hr, i, iframe, img, li, ol, p, pre, s, strong, u, ul, video
        """
        # Если контент уже содержит HTML-теги, возвращаем как есть (но с валидацией)
        if '<' in content and '>' in content:
            return content
        
        # Если это Markdown или простой текст - конвертируем в разрешенные HTML теги
        lines = content.strip().split('\n')
        html_lines = []
        
        in_list = False
        list_type = None  # 'ul' или 'ol'
        
        for line in lines:
            line = line.strip()
            if not line:
                # Закрываем список если он был открыт
                if in_list:
                    html_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                html_lines.append('<br>')
            elif line.startswith('## '):  # ## -> h3
                # Закрываем список если он был открыт
                if in_list:
                    html_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                html_lines.append(f'<h3>{line[3:]}</h3>')
            elif line.startswith('### '):  # ### -> h4
                # Закрываем список если он был открыт
                if in_list:
                    html_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                html_lines.append(f'<h4>{line[4:]}</h4>')
            elif line.startswith('**') and line.endswith('**'):  # **текст** -> <strong>
                # Закрываем список если он был открыт
                if in_list:
                    html_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                html_lines.append(f'<strong>{line[2:-2]}</strong>')
            elif line.startswith('*') and line.endswith('*'):  # *текст* -> <em>
                # Закрываем список если он был открыт
                if in_list:
                    html_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                html_lines.append(f'<em>{line[1:-1]}</em>')
            elif line.startswith('- '):  # - пункт -> <li>
                if not in_list:
                    html_lines.append('<ul>')
                    in_list = True
                    list_type = 'ul'
                html_lines.append(f'<li>{line[2:]}</li>')
            elif line.startswith('1. '):  # 1. пункт -> <li>
                if not in_list:
                    html_lines.append('<ol>')
                    in_list = True
                    list_type = 'ol'
                html_lines.append(f'<li>{line[3:]}</li>')
            else:  # Обычный текст -> <p>
                # Закрываем список если он был открыт
                if in_list:
                    html_lines.append(f'</{list_type}>')
                    in_list = False
                    list_type = None
                html_lines.append(f'<p>{line}</p>')
        
        # Закрываем открытый список в конце
        if in_list:
            html_lines.append(f'</{list_type}>')
        
        return ''.join(html_lines)

# Глобальный экземпляр сервиса
telegraph_service = TelegraphService()